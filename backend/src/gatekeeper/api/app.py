"""FastAPI app factory: the webhook gateway, the extension API (``/api``) and health
endpoints.

Local only: the server binds to API_HOST (default 127.0.0.1). Interactive API docs are off.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from gatekeeper.api.extension import ExtensionDeps, build_router
from gatekeeper.api.webhooks import WebhookStore, process_delivery, signature_valid
from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import (
    AuthenticationFailed,
    ConfigError,
    ErrorCode,
    GatekeeperError,
    PayloadTooLarge,
    ValidationFailed,
)
from gatekeeper.logging import log_context

Readiness = Callable[[], dict[str, str]]
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
STATUS_BY_CODE: Final = {
    ErrorCode.CONFIG: 503,
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.AUTHENTICATION_FAILED: 401,
    ErrorCode.POLICY_VIOLATION: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.EXTERNAL_SERVICE: 503,
    ErrorCode.IDENTITY_LEAK_BLOCKED: 403,
    ErrorCode.PRIVATE_REPO_HOSTED_MODEL_BLOCKED: 403,
    ErrorCode.INPUT_TOO_LARGE: 413,
    ErrorCode.MODEL_OUTPUT_INVALID: 502,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.GEMINI_KEYS_INVALID: 503,
    ErrorCode.MODEL_TIMEOUT: 504,
    ErrorCode.GITHUB_WRITE_FORBIDDEN: 403,
}


class RequestIdMiddleware:
    """Binds a request id (incoming ``X-Request-ID`` if sane, else new) into logging."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid4().hex

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        with log_context(request_id=request_id):
            await self.app(scope, receive, send_with_id)


def error_response(error: GatekeeperError) -> JSONResponse:
    """The error model's public dict only; internal details never leave the process."""
    try:
        status = STATUS_BY_CODE.get(ErrorCode(error.code), 500)
    except ValueError:
        status = 500
    return JSONResponse({"error": error.to_public_dict()}, status_code=status)


async def _gatekeeper_error(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, GatekeeperError):
        return error_response(exc)
    return JSONResponse({"error": {"code": "internal_error"}}, status_code=500)


async def _invalid_request(_request: Request, exc: Exception) -> JSONResponse:
    """Request validation errors in the error model's shape (field names only)."""
    fields = []
    if isinstance(exc, RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
    error = ValidationFailed(f"invalid request: {', '.join(fields) or 'malformed input'}")
    return JSONResponse({"error": error.to_public_dict()}, status_code=400)


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge(f"body larger than {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise PayloadTooLarge(f"body larger than {limit} bytes")
    return bytes(body)


def create_app(
    settings: Settings | None = None,
    *,
    store: WebhookStore | None = None,
    readiness: Readiness | None = None,
    extension: ExtensionDeps | None = None,
) -> FastAPI:
    """Build the gateway. Refuses to start without GITHUB_WEBHOOK_SECRET."""
    settings = settings or get_settings()
    settings.require("GITHUB_WEBHOOK_SECRET")
    secret = (
        settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else ""
    )
    if not secret:
        raise ConfigError("missing required setting(s): GITHUB_WEBHOOK_SECRET")
    if store is None or readiness is None:
        from gatekeeper.api.deps import default_extension, default_readiness, default_store

        store = store or default_store(settings)
        readiness = readiness or default_readiness(settings)
        extension = extension or default_extension(settings)
    webhook_store, check_ready = store, readiness

    app = FastAPI(
        title="Gatekeeper",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        exception_handlers={
            GatekeeperError: _gatekeeper_error,
            RequestValidationError: _invalid_request,
        },
    )
    app.add_middleware(RequestIdMiddleware)
    if extension is not None:
        app.include_router(build_router(extension), prefix="/api")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        components = await run_in_threadpool(check_ready)
        ready = all(state in {"ok", "not configured"} for state in components.values())
        return JSONResponse(
            {"status": "ready" if ready else "not ready", "components": components},
            status_code=200 if ready else 503,
        )

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        body = await _read_limited(request, settings.webhook_max_body_bytes)
        if not signature_valid(secret, body, request.headers.get("x-hub-signature-256")):
            raise AuthenticationFailed("missing or invalid X-Hub-Signature-256")
        event = request.headers.get("x-github-event")
        delivery_id = request.headers.get("x-github-delivery")
        if not event or not delivery_id:
            raise ValidationFailed("X-GitHub-Event and X-GitHub-Delivery headers are required")
        outcome = await run_in_threadpool(
            process_delivery, settings, webhook_store, event, delivery_id, body
        )
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    return app

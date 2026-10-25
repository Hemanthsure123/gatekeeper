"""Gemini API driver for hosted Gemma (optional extra ``gemini``: google-genai), with a
rotating key pool (``gatekeeper.models.key_pool``).

Imported lazily so Ollama mode works without the extra installed.

From the current docs (Gemma on the Gemini API, API errors, google-genai 2.33):
- confirmed for Gemma 4: system instructions, image input, thinking control via
  ``thinking_level`` ("high" = on, "minimal" = off);
- NOT confirmed for Gemma: native structured output. The request still sets
  ``response_mime_type="application/json"`` and ``response_json_schema``; the adapter
  also states the schema in the system text and always validates with Pydantic;
- errors: 429 RESOURCE_EXHAUSTED (rate limit), 401 (invalid key), 403
  PERMISSION_DENIED (key lacks access), 400 with API_KEY_INVALID, 5xx/408 transient;
- context window: read from the model's ``input_token_limit`` (models.get); if that
  lookup fails, ``FALLBACK_CONTEXT_TOKENS`` (128K, the smallest documented Gemma 4
  window) is used.

Key handling: a rate-limited key cools down and the SAME request moves to the next
healthy key without using an adapter retry (bounded by the pool size); an invalid key
is dropped for the process; timeouts and 5xx go through the adapter's retries and the
next attempt prefers another key. Keys are referred to only by alias.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, Final, TypeVar

import httpx

from gatekeeper.config import Settings
from gatekeeper.errors import (
    ConfigError,
    ExternalServiceError,
    GatekeeperError,
    ModelTimeout,
    ModelUnavailable,
)
from gatekeeper.logging import get_logger
from gatekeeper.models.backend import BackendCall, Completion
from gatekeeper.models.key_pool import KeyLease, KeyPool
from gatekeeper.models.ollama import parse_retry_after
from gatekeeper.models.types import (
    BackendHealth,
    Capabilities,
    ImagePart,
    ModelRole,
    Reasoning,
    TextPart,
)

R = TypeVar("R")
RETRYABLE_STATUS: Final = frozenset({408, 500, 502, 503, 504})
INVALID_KEY_STATUS: Final = frozenset({401, 403})
FALLBACK_CONTEXT_TOKENS: Final = 131_072
_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")

log = get_logger("gatekeeper.models.gemini")


class _RateLimitedError(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class _InvalidKeyError(Exception):
    pass


def retry_delay(details: Any) -> float | None:
    """Find a google.rpc.RetryInfo ``retryDelay`` (e.g. "30s") anywhere in error details."""
    if isinstance(details, Mapping):
        value = details.get("retryDelay")
        if isinstance(value, str) and (match := _RETRY_DELAY.match(value)):
            return float(match[1])
        details = list(details.values())
    if isinstance(details, list):
        for item in details:
            found = retry_delay(item)
            if found is not None:
                return found
    return None


class GeminiBackend:
    """BackendDriver for Gemma models on the Gemini API."""

    def __init__(
        self,
        settings: Settings,
        *,
        pool: KeyPool | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        settings.require("GEMMA_MODEL_PRIMARY")
        if not settings.gemini_keys():
            raise ConfigError(
                "missing required setting(s): GEMINI_API_KEYS (or the legacy GEMINI_API_KEY)"
            )
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as exc:
            raise ConfigError(
                "the gemini_api backend needs the optional 'gemini' extra: "
                "run `uv sync --extra gemini`"
            ) from exc
        self._types: Any = types
        self._errors: Any = errors
        self._primary = settings.gemma_model_primary
        self._secondary = settings.gemma_model_secondary
        self._timeout_s = settings.model_timeout_s
        timeout_ms = int(settings.model_timeout_s * 1000)
        self._factory = client_factory or (
            lambda key: genai.Client(
                api_key=key, http_options=types.HttpOptions(timeout=timeout_ms)
            )
        )
        self.pool = pool or KeyPool.from_settings(settings)
        self._clients: dict[str, Any] = {}
        self._context: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "gemini_api"

    def model_for(self, role: ModelRole) -> str:
        if role is ModelRole.PRIMARY and self._primary:
            return self._primary
        if role is ModelRole.SECONDARY and self._secondary:
            return self._secondary
        key = "GEMMA_MODEL_PRIMARY" if role is ModelRole.PRIMARY else "GEMMA_MODEL_SECONDARY"
        raise ConfigError(f"missing required setting(s): {key} (needed for the {role} role)")

    # -- keys -----------------------------------------------------------------------------

    def _client(self, lease: KeyLease) -> Any:
        if lease.alias not in self._clients:
            self._clients[lease.alias] = self._factory(lease.secret.get_secret_value())
        return self._clients[lease.alias]

    def with_key(self, func: Callable[[Any], R]) -> tuple[R, str, int]:
        """Run ``func(client)`` on a pool key with failover: (result, alias, failovers)."""
        failovers = 0
        while True:
            lease = self.pool.acquire()
            try:
                result = self._call(partial(func, self._client(lease)))
            except _RateLimitedError as exc:
                self.pool.rate_limited(lease.alias, exc.retry_after)
                failovers += 1
                if failovers >= self.pool.size:
                    raise ModelUnavailable(
                        "every Gemini API key was rate limited for this request",
                        retry_after=exc.retry_after,
                        details={"failovers": failovers},
                    ) from None
                log.warning("gemini key failover", from_key=lease.alias, reason="rate_limited")
                continue
            except _InvalidKeyError:
                self.pool.invalid(lease.alias)
                failovers += 1
                log.warning("gemini key failover", from_key=lease.alias, reason="invalid_key")
                continue
            except (ModelUnavailable, ModelTimeout):
                self.pool.transient(lease.alias)
                raise
            self.pool.success(lease.alias)
            return result, lease.alias, failovers

    # -- capabilities and health ------------------------------------------------------------

    def context_tokens(self, model: str) -> int:
        if model not in self._context:
            try:
                info, _alias, _failovers = self.with_key(lambda c: c.models.get(model=model))
                limit = getattr(info, "input_token_limit", None)
            except (ModelUnavailable, ModelTimeout, ExternalServiceError) as exc:
                log.warning(
                    "could not read the model's context window; using the fallback",
                    code=exc.code,
                    fallback=FALLBACK_CONTEXT_TOKENS,
                )
                return FALLBACK_CONTEXT_TOKENS
            self._context[model] = (
                limit if isinstance(limit, int) and limit > 0 else FALLBACK_CONTEXT_TOKENS
            )
        return self._context[model]

    def capabilities(self, model: str | None = None) -> Capabilities:
        return Capabilities(
            supports_images=True,
            supports_structured_output=True,
            supports_reasoning_control=True,
            max_context_tokens=self.context_tokens(model or self.model_for(ModelRole.PRIMARY)),
        )

    def health(self) -> BackendHealth:
        model = self.model_for(ModelRole.PRIMARY)
        try:
            _info, alias, failovers = self.with_key(lambda c: c.models.get(model=model))
        except GatekeeperError as exc:
            return BackendHealth(ok=False, message=f"Gemini API check for {model} failed: {exc}")
        return BackendHealth(
            ok=True,
            message=f"Gemini API reachable; model {model} available (via {alias})",
            details={"key_alias": alias, "failovers": failovers, "pool": self.pool.status()},
        )

    # -- calls ----------------------------------------------------------------------------

    def complete(self, call: BackendCall) -> Completion:
        types = self._types
        parts = [
            types.Part.from_text(text=part.text)
            if isinstance(part, TextPart)
            else types.Part.from_bytes(data=part.data, mime_type=part.mime_type)
            for part in call.parts
            if isinstance(part, TextPart | ImagePart)
        ]
        contents = [types.Content(role="user", parts=parts)] + [
            types.Content(
                role="model" if turn.role == "assistant" else "user",
                parts=[types.Part.from_text(text=turn.text)],
            )
            for turn in call.followups
        ]
        level = (
            types.ThinkingLevel.HIGH
            if call.reasoning is Reasoning.ON
            else types.ThinkingLevel.MINIMAL
        )
        config = types.GenerateContentConfig(
            system_instruction=call.system,
            temperature=call.temperature,
            seed=call.seed,
            max_output_tokens=call.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=call.json_schema,
            thinking_config=types.ThinkingConfig(thinking_level=level),
        )
        response, alias, failovers = self.with_key(
            lambda client: client.models.generate_content(
                model=call.model, contents=contents, config=config
            )
        )
        usage = getattr(response, "usage_metadata", None)
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        return Completion(
            text=response.text or "",
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            finish_reason=str(finish) if finish is not None else None,
            meta={"key_alias": alias, "failovers": failovers},
        )

    def _call(self, func: Callable[[], R]) -> R:
        try:
            return func()
        except self._errors.APIError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            status = str(getattr(exc, "status", None) or "error")
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                raise _RateLimitedError(_retry_after(exc)) from None
            if code in INVALID_KEY_STATUS or _is_invalid_key(exc):
                raise _InvalidKeyError() from None
            if code in RETRYABLE_STATUS:
                raise ModelUnavailable(
                    f"Gemini API returned {code} {status}",
                    retry_after=_retry_after(exc),
                    details={"status": code},
                ) from None
            raise ExternalServiceError(
                f"Gemini API rejected the request ({code} {status})",
                retryable=False,
                details={"status": code},
            ) from None
        except httpx.TimeoutException:
            raise ModelTimeout(f"Gemini API did not answer within {self._timeout_s:g}s") from None
        except httpx.TransportError as exc:
            raise ModelUnavailable(f"cannot reach the Gemini API ({type(exc).__name__})") from None


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    header = headers.get("retry-after") if headers is not None else None
    return parse_retry_after(header) or retry_delay(getattr(exc, "details", None))


def _is_invalid_key(exc: Any) -> bool:
    code = int(getattr(exc, "code", 0) or 0)
    if code != 400:
        return False
    text = json.dumps(getattr(exc, "details", None), default=str) + str(getattr(exc, "message", ""))
    return "API_KEY_INVALID" in text or "API key not valid" in text

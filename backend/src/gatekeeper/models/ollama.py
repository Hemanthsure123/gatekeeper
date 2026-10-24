"""Ollama driver (REST API via httpx), verified against the Ollama v0.40 API docs.

- ``POST /api/chat`` with ``stream: false``, ``format`` = JSON schema (structured
  outputs), ``options`` = num_ctx / temperature / seed / num_predict, ``keep_alive``,
  base64 ``images`` on the user message, and ``think`` only when ``/api/show`` says the
  model supports thinking.
- num_ctx is always sent explicitly: Ollama's default context is only 4096 tokens on
  machines with < 24 GiB VRAM, which would silently truncate long inputs.
- ``POST /api/show`` gives capabilities (``vision``, ``thinking``), thinking values and
  the model's native ``*.context_length``. ``GET /api/tags`` lists installed models.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, ExternalServiceError, ModelTimeout, ModelUnavailable
from gatekeeper.models.backend import BackendCall, Completion
from gatekeeper.models.types import (
    BackendHealth,
    Capabilities,
    ImagePart,
    ModelRole,
    Reasoning,
    TextPart,
)

CONNECT_TIMEOUT_S: Final = 10.0
MAX_ERROR_CHARS: Final = 200


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After as seconds (delta-seconds or HTTP-date); None when absent or invalid."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _keep_alive(value: str) -> str | int:
    """Ollama accepts a duration string or a number of seconds."""
    try:
        return int(value)
    except ValueError:
        return value


def _model_names(tags: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in tags.get("models") or []:
        if isinstance(entry, Mapping):
            names.update(str(entry[k]) for k in ("name", "model") if entry.get(k))
    return names


def _installed(model: str, names: set[str]) -> bool:
    return model in names or (":" not in model and f"{model}:latest" in names)


class OllamaBackend:
    """BackendDriver for a local Ollama server."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        settings.require("OLLAMA_BASE_URL", "OLLAMA_MODEL")
        self._base_url = (settings.ollama_base_url or "").rstrip("/")
        self._primary = settings.ollama_model
        self._secondary = settings.ollama_model_secondary
        self._num_ctx = settings.ollama_num_ctx
        self._keep_alive = _keep_alive(settings.ollama_keep_alive)
        self._timeout_s = settings.model_timeout_s
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.model_timeout_s, connect=CONNECT_TIMEOUT_S),
        )
        self._show_cache: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "ollama"

    def model_for(self, role: ModelRole) -> str:
        if role is ModelRole.PRIMARY and self._primary:
            return self._primary
        if role is ModelRole.SECONDARY and self._secondary:
            return self._secondary
        key = "OLLAMA_MODEL" if role is ModelRole.PRIMARY else "OLLAMA_MODEL_SECONDARY"
        raise ConfigError(f"missing required setting(s): {key} (needed for the {role} role)")

    # -- metadata -----------------------------------------------------------------------

    def _show(self, model: str) -> dict[str, Any]:
        if model not in self._show_cache:
            self._show_cache[model] = self._request("POST", "/api/show", model, {"model": model})
        return self._show_cache[model]

    @staticmethod
    def _native_context(show: Mapping[str, Any]) -> int | None:
        info = show.get("model_info")
        if isinstance(info, Mapping):
            for key, value in info.items():
                if str(key).endswith(".context_length") and isinstance(value, int):
                    return value
        return None

    @staticmethod
    def _think_value(show: Mapping[str, Any], reasoning: Reasoning) -> bool | str | None:
        """Value for ``think``, or None to omit it (model has no thinking control)."""
        thinking = show.get("thinking")
        if isinstance(thinking, Mapping):
            values = list(thinking.get("values") or [])
            if reasoning is Reasoning.ON:
                if True in values:
                    return True
                named = [v for v in values if isinstance(v, str)]
                return "high" if "high" in named else (named[-1] if named else None)
            return False if False in values else None
        if "thinking" in (show.get("capabilities") or []):
            return reasoning is Reasoning.ON
        return None

    def capabilities(self, model: str | None = None) -> Capabilities:
        show = self._show(model or self.model_for(ModelRole.PRIMARY))
        caps = show.get("capabilities") or []
        native = self._native_context(show)
        return Capabilities(
            supports_images="vision" in caps,
            supports_structured_output=True,
            supports_reasoning_control=self._think_value(show, Reasoning.OFF) is not None,
            max_context_tokens=min(self._num_ctx, native) if native else self._num_ctx,
        )

    def health(self) -> BackendHealth:
        try:
            tags = self._request("GET", "/api/tags", None, None)
        except (ModelUnavailable, ModelTimeout, ExternalServiceError) as exc:
            return BackendHealth(
                ok=False,
                message=f"Ollama is not responding at {self._base_url} ({exc.message}). "
                "Start Ollama and try again.",
            )
        names = _model_names(tags)
        wanted = [m for m in (self._primary, self._secondary) if m]
        missing = [m for m in wanted if not _installed(m, names)]
        if missing:
            pulls = "; ".join(f"ollama pull {m}" for m in missing)
            return BackendHealth(
                ok=False,
                message=f"model(s) not installed: {', '.join(missing)}. Run: {pulls}",
                details={"installed": sorted(names)},
            )
        return BackendHealth(
            ok=True,
            message=f"Ollama at {self._base_url} has {', '.join(wanted)}",
            details={"installed": sorted(names)},
        )

    # -- calls --------------------------------------------------------------------------

    def complete(self, call: BackendCall) -> Completion:
        texts = [part.text for part in call.parts if isinstance(part, TextPart)]
        images = [part for part in call.parts if isinstance(part, ImagePart)]
        user: dict[str, Any] = {"role": "user", "content": "\n\n".join(texts)}
        if images:
            user["images"] = [base64.b64encode(image.data).decode("ascii") for image in images]
        payload: dict[str, Any] = {
            "model": call.model,
            "messages": [
                {"role": "system", "content": call.system},
                user,
                *({"role": turn.role, "content": turn.text} for turn in call.followups),
            ],
            "stream": False,
            "format": call.json_schema,
            "options": {
                "num_ctx": self._num_ctx,
                "temperature": call.temperature,
                "seed": call.seed,
                "num_predict": call.max_output_tokens,
            },
            "keep_alive": self._keep_alive,
        }
        think = self._think_value(self._show(call.model), call.reasoning)
        if think is not None:
            payload["think"] = think

        data = self._request("POST", "/api/chat", call.model, payload)
        message = data.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ExternalServiceError(
                "Ollama returned a reply without message content", retryable=False
            )
        return Completion(
            text=content,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            finish_reason=data.get("done_reason"),
        )

    def _request(
        self, method: str, path: str, model: str | None, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(f"Ollama did not answer within {self._timeout_s:g}s") from exc
        except httpx.TransportError as exc:
            raise ModelUnavailable(
                f"cannot reach Ollama at {self._base_url} ({type(exc).__name__})"
            ) from exc

        status = response.status_code
        if status >= 400:
            detail = _error_text(response)
            if status == 429 or status >= 500:
                raise ModelUnavailable(
                    f"Ollama returned HTTP {status}: {detail}",
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                    details={"status": status},
                )
            if status == 404 and model:
                raise ModelUnavailable(
                    f"Ollama model {model!r} not found; run: ollama pull {model}",
                    retryable=False,
                    details={"status": status},
                )
            raise ExternalServiceError(
                f"Ollama rejected the request (HTTP {status}): {detail}",
                retryable=False,
                details={"status": status},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "Ollama returned a non-JSON response", retryable=False
            ) from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(
                "Ollama returned an unexpected response shape", retryable=False
            )
        return data


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    message = body.get("error") if isinstance(body, Mapping) else None
    return str(message)[:MAX_ERROR_CHARS] if message else (response.reason_phrase or "error")

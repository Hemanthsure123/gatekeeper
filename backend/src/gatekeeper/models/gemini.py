"""Gemini API driver for hosted Gemma (optional extra ``gemini``: google-genai).

Imported lazily so Ollama mode works without the extra installed.

From the current docs (Gemma on the Gemini API, google-genai 2.33):
- confirmed for Gemma 4: system instructions, image input, thinking control via
  ``thinking_level`` ("high" = on, "minimal" = off);
- NOT confirmed for Gemma: native structured output. The request still sets
  ``response_mime_type="application/json"`` and ``response_json_schema``; the adapter
  also states the schema in the system text and always validates with Pydantic.
- Context length via the API is not documented here, so ``max_context_tokens`` is
  None; the API rejects oversize requests with a 400 instead of truncating.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

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

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


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

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        settings.require("GEMINI_API_KEY", "GEMMA_MODEL_PRIMARY")
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
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        self._client: Any = client or genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=int(settings.model_timeout_s * 1000)),
        )

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

    def capabilities(self, model: str | None = None) -> Capabilities:
        return Capabilities(
            supports_images=True,
            supports_structured_output=True,
            supports_reasoning_control=True,
            max_context_tokens=None,
        )

    def health(self) -> BackendHealth:
        model = self.model_for(ModelRole.PRIMARY)
        try:
            self._call(lambda: self._client.models.get(model=model))
        except (ModelUnavailable, ModelTimeout, ExternalServiceError) as exc:
            return BackendHealth(ok=False, message=f"Gemini API check for {model} failed: {exc}")
        return BackendHealth(ok=True, message=f"Gemini API reachable; model {model} available")

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
        response = self._call(
            lambda: self._client.models.generate_content(
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
        )

    def _call(self, func: Any) -> Any:
        try:
            return func()
        except self._errors.APIError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            status = getattr(exc, "status", None) or "error"
            if code in RETRYABLE_STATUS:
                raise ModelUnavailable(
                    f"Gemini API returned {code} {status}",
                    retry_after=retry_delay(getattr(exc, "details", None)),
                    details={"status": code},
                ) from exc
            raise ExternalServiceError(
                f"Gemini API rejected the request ({code} {status})",
                retryable=False,
                details={"status": code},
            ) from exc
        except httpx.TimeoutException as exc:
            raise ModelTimeout(f"Gemini API did not answer within {self._timeout_s:g}s") from exc
        except httpx.TransportError as exc:
            raise ModelUnavailable(f"cannot reach the Gemini API ({type(exc).__name__})") from exc

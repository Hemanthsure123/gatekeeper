"""The shared wrapper every backend gets: validation, size checks, concurrency, retries,
structured output with one repair attempt, caching, audit and logging.

Never logs prompt or response content. Audit events carry content (redacted by the
audit Redactor) but images only as hash, MIME type and size.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from gatekeeper.audit import Actor, EventType, Run
from gatekeeper.audit.hashing import canonical_json, sha256_hex
from gatekeeper.config import Settings
from gatekeeper.errors import (
    CapabilityMissing,
    GatekeeperError,
    InputTooLarge,
    ModelOutputInvalid,
    PolicyViolation,
)
from gatekeeper.logging import get_logger
from gatekeeper.models.backend import BackendCall, BackendDriver, Completion, Turn
from gatekeeper.models.cache import ModelCache, cache_key
from gatekeeper.models.types import (
    BackendHealth,
    CachePolicy,
    Capabilities,
    ModelRequest,
    ModelResponse,
    ModelRole,
    Usage,
)

RequestValidator = Callable[[ModelRequest[Any]], None]

#: Conservative estimate: ~3 UTF-8 bytes per token over-counts English and code and is
#: about right for CJK, so the pre-check errs towards refusing rather than truncating.
BYTES_PER_TOKEN: Final = 3
#: Upper-bound guess for one image's token cost; images are small in this project.
IMAGE_TOKENS: Final = 1024
#: Chat template / role markers overhead.
OVERHEAD_TOKENS: Final = 64
BACKOFF_BASE_S: Final = 1.0
BACKOFF_CAP_S: Final = 30.0
RETRY_AFTER_CAP_S: Final = 300.0
MAX_ERRORS_IN_REPAIR: Final = 5

SCHEMA_INSTRUCTION: Final = (
    "Reply with exactly one JSON object that matches the JSON schema below. "
    "Output only that JSON object, with no other text.\nJSON schema:\n"
)

log = get_logger("gatekeeper.models")

_semaphores: dict[int, threading.BoundedSemaphore] = {}
_semaphores_lock = threading.Lock()


def process_semaphore(size: int) -> threading.BoundedSemaphore:
    """Per-process limit on concurrent model calls (a cross-process limit comes later)."""
    with _semaphores_lock:
        return _semaphores.setdefault(size, threading.BoundedSemaphore(size))


def compose_system(system: str, json_schema: dict[str, Any]) -> str:
    """Append the schema to the system text; Ollama recommends grounding the schema in the
    prompt as well as sending it as the native format constraint."""
    schema_text = json.dumps(json_schema, separators=(",", ":"), sort_keys=True)
    return f"{system.rstrip()}\n\n{SCHEMA_INSTRUCTION}{schema_text}".lstrip()


def estimate_tokens(texts: Iterable[str], image_count: int = 0) -> int:
    size = sum(len(text.encode("utf-8")) for text in texts)
    return math.ceil(size / BYTES_PER_TOKEN) + image_count * IMAGE_TOKENS + OVERHEAD_TOKENS


def summarize_validation_error(error: ValidationError) -> str:
    items = error.errors(include_input=False, include_url=False)
    shown = [
        f"{'.'.join(str(p) for p in item['loc']) or '<root>'}: {item['msg']}"
        for item in items[:MAX_ERRORS_IN_REPAIR]
    ]
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def repair_message(summary: str) -> str:
    return (
        "Your previous reply did not match the required JSON schema "
        f"({summary}). Reply again with only one JSON object that matches the schema."
    )


class ModelAdapter:
    """Implements :class:`~gatekeeper.models.backend.ModelBackend` over one driver."""

    def __init__(
        self,
        driver: BackendDriver,
        *,
        max_retries: int = 2,
        max_concurrency: int = 1,
        cache: ModelCache | None = None,
        validators: Iterable[RequestValidator] = (),
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._driver = driver
        self._max_retries = max_retries
        self._semaphore = process_semaphore(max_concurrency)
        self._cache = cache
        self._validators = list(validators)
        self._sleep = sleep
        self._rand = rand
        self._clock = clock

    @classmethod
    def from_settings(
        cls, driver: BackendDriver, settings: Settings, cache: ModelCache | None = None
    ) -> ModelAdapter:
        return cls(
            driver,
            max_retries=settings.model_max_retries,
            max_concurrency=settings.model_max_concurrency,
            cache=cache,
        )

    @property
    def backend_name(self) -> str:
        return self._driver.name

    def model_name(self, role: ModelRole = ModelRole.PRIMARY) -> str:
        return self._driver.model_for(role)

    def with_validators(self, *validators: RequestValidator) -> ModelAdapter:
        """A copy with extra validators (for example a per-run leak guard). Shares the
        driver, cache and concurrency limit; this adapter is not changed."""
        copy = ModelAdapter.__new__(ModelAdapter)
        copy.__dict__.update(self.__dict__)
        copy._validators = [*self._validators, *validators]
        return copy

    def add_validator(self, validator: RequestValidator) -> None:
        """Register a pre-send check. It may raise PolicyViolation to block the call."""
        self._validators.append(validator)

    def health(self) -> BackendHealth:
        return self._driver.health()

    def capabilities(self) -> Capabilities:
        return self._driver.capabilities()

    def generate[T: BaseModel](
        self, request: ModelRequest[T], run: Run | None = None, *, allow_unaudited: bool = False
    ) -> ModelResponse[T]:
        if run is None and not allow_unaudited:
            raise PolicyViolation(
                "model calls must be audited: pass a run, or set allow_unaudited=True explicitly"
            )
        if run is None:
            log.warning("unaudited model call", prompt_id=request.prompt_id)
        for validator in self._validators:
            validator(request)

        model = self._driver.model_for(request.role)
        capabilities = self._driver.capabilities(model)
        if request.images and not capabilities.supports_images:
            raise CapabilityMissing(
                f"backend {self._driver.name!r} model {model!r} does not accept image input"
            )
        schema = request.response_model.model_json_schema()
        call = BackendCall(
            model=model,
            system=compose_system(request.system, schema),
            parts=request.parts,
            json_schema=schema,
            temperature=request.temperature,
            seed=request.seed,
            max_output_tokens=request.max_output_tokens,
            reasoning=request.reasoning,
        )
        estimated_tokens = self._check_input_size(call, request, capabilities)
        key = cache_key(backend=self._driver.name, model=model, request=request, json_schema=schema)

        request_seq = (
            self._audit_request(run, request, model, schema, capabilities)
            if run is not None
            else None
        )
        started = self._clock()
        try:
            response = None
            if request.cache_policy is CachePolicy.USE:
                response = self._read_cache(key, request, started)
            if response is None:
                response = self._call(call, request, capabilities, started)
        except GatekeeperError as exc:
            if run is not None and request_seq is not None:
                run.record(
                    EventType.ERROR,
                    Actor.SYSTEM,
                    {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                    parent_seq=request_seq,
                )
            raise

        if run is not None and request_seq is not None:
            self._audit_response(run, request_seq, response)
        if not response.cache_hit and request.cache_policy is not CachePolicy.BYPASS:
            self._write_cache(key, response)
        log.info(
            "model call complete",
            backend=response.backend,
            model=response.model,
            prompt_id=request.prompt_id,
            latency_ms=round(response.latency_ms, 1),
            # Logged side by side to calibrate the conservative estimate (ADR 0003).
            estimated_input_tokens=estimated_tokens,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_hit=response.cache_hit,
            attempts=response.attempts,
        )
        return response

    # -- size ---------------------------------------------------------------------------

    def _check_input_size(
        self, call: BackendCall, request: ModelRequest[Any], capabilities: Capabilities
    ) -> int:
        """Return the input token estimate; raise InputTooLarge if it cannot fit."""
        texts = [call.system, *request.text_parts, *(turn.text for turn in call.followups)]
        estimate = estimate_tokens(texts, len(request.images))
        limit = capabilities.max_context_tokens
        if limit is None:
            return estimate
        budget = limit - request.max_output_tokens
        if estimate > budget:
            raise InputTooLarge(
                f"input is about {estimate} tokens but only {budget} fit "
                f"(context {limit} minus output budget {request.max_output_tokens})",
                details={"estimate": estimate, "context": limit},
            )
        return estimate

    def _check_truncation(
        self, completion: Completion, request: ModelRequest[Any], capabilities: Capabilities
    ) -> None:
        limit = capabilities.max_context_tokens
        used = completion.input_tokens
        if limit is None or used is None:
            return
        if used >= limit - request.max_output_tokens:
            raise InputTooLarge(
                f"backend reported {used} prompt tokens, at the context limit ({limit}) "
                "minus the output budget; the input may have been silently truncated",
                details={"input_tokens": used, "context": limit},
            )

    # -- calling ------------------------------------------------------------------------

    def _call[T: BaseModel](
        self,
        call: BackendCall,
        request: ModelRequest[T],
        capabilities: Capabilities,
        started: float,
    ) -> ModelResponse[T]:
        completion, attempts = self._complete_with_retries(call)
        self._check_truncation(completion, request, capabilities)
        parsed, problem = self._parse(request.response_model, completion.text)
        input_tokens, output_tokens = completion.input_tokens, completion.output_tokens

        if parsed is None:
            repair = replace(
                call,
                followups=(
                    Turn("assistant", completion.text),
                    Turn("user", repair_message(problem or "invalid output")),
                ),
            )
            self._check_input_size(repair, request, capabilities)
            completion, more = self._complete_with_retries(repair)
            attempts += more
            self._check_truncation(completion, request, capabilities)
            parsed, problem = self._parse(request.response_model, completion.text)
            input_tokens = _add(input_tokens, completion.input_tokens)
            output_tokens = _add(output_tokens, completion.output_tokens)
            if parsed is None:
                raise ModelOutputInvalid(
                    "model output did not match the response schema after one repair attempt",
                    details={"prompt_id": request.prompt_id, "problem": problem},
                )

        return ModelResponse(
            parsed=parsed,
            raw_text=completion.text,
            model=call.model,
            backend=self._driver.name,
            usage=Usage(input_tokens, output_tokens),
            latency_ms=(self._clock() - started) * 1000,
            cache_hit=False,
            attempts=attempts,
        )

    def _complete_with_retries(self, call: BackendCall) -> tuple[Completion, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._semaphore:
                    return self._driver.complete(call), attempt
            except GatekeeperError as exc:
                if not exc.retryable or attempt > self._max_retries:
                    raise
                delay = self._backoff(attempt, getattr(exc, "retry_after", None))
                log.warning(
                    "model call failed; retrying",
                    code=exc.code,
                    attempt=attempt,
                    delay_s=round(delay, 2),
                )
                self._sleep(delay)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter; a server Retry-After is a lower bound."""
        ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0 ** (attempt - 1))
        delay = ceiling / 2 + self._rand() * ceiling / 2
        if retry_after is not None and retry_after > 0:
            delay = max(delay, min(retry_after, RETRY_AFTER_CAP_S))
        return delay

    @staticmethod
    def _parse[T: BaseModel](model: type[T], text: str) -> tuple[T | None, str | None]:
        try:
            return model.model_validate_json(text), None
        except ValidationError as exc:
            return None, summarize_validation_error(exc)

    # -- cache --------------------------------------------------------------------------

    def _read_cache[T: BaseModel](
        self, key: str, request: ModelRequest[T], started: float
    ) -> ModelResponse[T] | None:
        if self._cache is None:
            return None
        raw = self._cache.get(key)
        if raw is None:
            return None
        try:
            entry = json.loads(raw)
            parsed = request.response_model.model_validate(entry["parsed"])
            return ModelResponse(
                parsed=parsed,
                raw_text=str(entry["raw_text"]),
                model=str(entry["model"]),
                backend=str(entry["backend"]),
                usage=Usage(entry.get("input_tokens"), entry.get("output_tokens")),
                latency_ms=(self._clock() - started) * 1000,
                cache_hit=True,
                attempts=0,
            )
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable model cache entry")
            return None

    def _write_cache(self, key: str, response: ModelResponse[Any]) -> None:
        if self._cache is None:
            return
        entry = {
            "v": 1,
            "raw_text": response.raw_text,
            "parsed": response.parsed.model_dump(mode="json"),
            "model": response.model,
            "backend": response.backend,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        self._cache.set(key, json.dumps(entry))

    # -- audit --------------------------------------------------------------------------

    def _audit_request(
        self,
        run: Run,
        request: ModelRequest[Any],
        model: str,
        schema: dict[str, Any],
        capabilities: Capabilities,
    ) -> int:
        event = run.record(
            EventType.MODEL_REQUEST,
            Actor.SYSTEM,
            {
                "prompt_id": request.prompt_id,
                "prompt_version": request.prompt_version,
                "backend": self._driver.name,
                "model": model,
                "role": request.role.value,
                "settings": {
                    "temperature": request.temperature,
                    "seed": request.seed,
                    "max_output_tokens": request.max_output_tokens,
                    "reasoning": request.reasoning.value,
                    "cache_policy": request.cache_policy.value,
                    "context_tokens": capabilities.max_context_tokens,
                },
                "response_schema_sha256": sha256_hex(canonical_json(schema)),
                "system": request.system,
                "text_parts": request.text_parts,
                "images": [
                    {"sha256": img.sha256, "mime_type": img.mime_type, "size_bytes": len(img.data)}
                    for img in request.images
                ],
            },
        )
        return event.seq

    @staticmethod
    def _audit_response(run: Run, request_seq: int, response: ModelResponse[Any]) -> None:
        run.record(
            EventType.MODEL_RESPONSE,
            Actor.MODEL,
            {
                "backend": response.backend,
                "model": response.model,
                "parsed": response.parsed.model_dump(mode="json"),
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                "latency_ms": round(response.latency_ms, 1),
                "cache_hit": response.cache_hit,
                "attempts": response.attempts,
            },
            parent_seq=request_seq,
        )


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)

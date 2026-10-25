"""Structured logging: structlog on top of stdlib logging.

- One pipeline for everything: third-party stdlib loggers are rendered by the same
  ``ProcessorFormatter`` as structlog loggers, so redaction applies to both.
- JSON lines outside development, a readable console format in development.
- ISO-8601 UTC timestamps.
- Context (request_id, job_id, run_id, item_id) travels via contextvars.
- Redaction runs last before rendering: sensitive keys are masked wholesale, then
  every string goes through the shared :class:`~gatekeeper.redaction.Redactor`.
- Request/response bodies are omitted unless ``log_bodies=True``.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import IO, Any, Final
from uuid import UUID

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from gatekeeper.config import Settings, get_settings
from gatekeeper.redaction import Redactor

SENSITIVE_KEY_PARTS: Final = ("token", "secret", "password", "key", "authorization", "cookie")
BODY_KEYS: Final = frozenset({"body", "request_body", "response_body"})
CONTEXT_KEYS: Final = frozenset({"request_id", "job_id", "run_id", "item_id"})
KEY_MASK: Final = "[REDACTED]"
BODY_MASK: Final = "[OMITTED]"

_HANDLER_NAME: Final = "gatekeeper"


_KEY_ALIAS = re.compile(r"key-\d{1,3}")


def _harmless(value: Any) -> bool:
    """Values that cannot be credentials even under a sensitive-looking field name:
    numbers, booleans, None (token counts) and key-pool aliases such as ``key-2``."""
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and _KEY_ALIAS.fullmatch(value) is not None


class RedactingProcessor:
    """structlog processor that masks sensitive keys and redacts every string value."""

    def __init__(self, redactor: Redactor, *, log_bodies: bool = False) -> None:
        self._redactor = redactor
        self._log_bodies = log_bodies

    def __call__(self, _logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        return {key: self._field(key, value) for key, value in event_dict.items()}

    def _field(self, key: str, value: Any) -> Any:
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS) and not _harmless(value):
            return KEY_MASK
        if not self._log_bodies and lowered in BODY_KEYS:
            return BODY_MASK
        return self._scrub(value)

    def _scrub(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            return {str(k): self._field(str(k), v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._scrub(item) for item in value]
        # Anything else is rendered as text later, so redact its text form now.
        return self._redactor.redact_text(value if isinstance(value, str) else str(value))


def _console_colors(stream: IO[str]) -> bool:
    # structlog needs colorama for colours on Windows; stay plain rather than fail.
    return sys.platform != "win32" and hasattr(stream, "isatty") and stream.isatty()


def configure_logging(
    settings: Settings | None = None,
    *,
    log_bodies: bool = False,
    stream: IO[str] | None = None,
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    settings = settings or get_settings()
    stream = stream or sys.stderr
    redactor = Redactor.from_settings(settings)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=_console_colors(stream))
        if settings.app_env == "development"
        else structlog.processors.JSONRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            RedactingProcessor(redactor, log_bodies=log_bodies),
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def _context_values(values: Mapping[str, str | UUID | None]) -> dict[str, str]:
    unknown = set(values) - CONTEXT_KEYS
    if unknown:
        raise ValueError(f"unsupported log context keys: {', '.join(sorted(unknown))}")
    return {key: str(value) for key, value in values.items() if value is not None}


def bind_context(**values: str | UUID | None) -> None:
    """Bind request_id / job_id / run_id / item_id for the current context."""
    structlog.contextvars.bind_contextvars(**_context_values(values))


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(**values: str | UUID | None) -> Iterator[None]:
    """Bind context keys for the duration of a ``with`` block, then restore."""
    with structlog.contextvars.bound_contextvars(**_context_values(values)):
        yield


def current_context() -> MutableMapping[str, Any]:
    return structlog.contextvars.get_contextvars()

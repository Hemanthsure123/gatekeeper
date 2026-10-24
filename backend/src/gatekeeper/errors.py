"""Gatekeeper error model.

Every error carries a stable, machine-readable ``code`` and a ``retryable`` flag.
``details`` and the exception cause are for logs and debugging only; they never
appear in :meth:`GatekeeperError.to_public_dict`, which is the only shape that may
leave the process (API responses, dashboard, extension).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    """Stable error codes. Additive only: never rename or reuse a value."""

    INTERNAL = "internal_error"
    CONFIG = "config_error"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    EXTERNAL_SERVICE = "external_service_error"
    AUDIT = "audit_error"
    POLICY_VIOLATION = "policy_violation"


class GatekeeperError(Exception):
    """Base class for all errors raised by Gatekeeper code."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    def to_public_dict(self) -> dict[str, Any]:
        """Return only fields that are safe to show outside the process."""
        return {"code": self.code, "message": self.message, "retryable": self.retryable}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class _CodedError(GatekeeperError):
    """A GatekeeperError whose code is fixed by its class."""

    error_code: ClassVar[ErrorCode]
    default_retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            self.error_code,
            message,
            retryable=self.default_retryable if retryable is None else retryable,
            details=details,
        )


class ConfigError(_CodedError):
    """Configuration is missing or invalid. Messages name keys, never values."""

    error_code = ErrorCode.CONFIG


class ValidationFailed(_CodedError):  # noqa: N818 - name fixed by the spec
    """Input or model output failed schema validation."""

    error_code = ErrorCode.VALIDATION_FAILED


class NotFoundError(_CodedError):
    """A requested entity does not exist."""

    error_code = ErrorCode.NOT_FOUND


class ExternalServiceError(_CodedError):
    """A dependency (GitHub, Ollama, Postgres, Redis) failed. Retryable by default."""

    error_code = ErrorCode.EXTERNAL_SERVICE
    default_retryable = True


class AuditError(_CodedError):
    """An audit event could not be written or verified. The step must not continue."""

    error_code = ErrorCode.AUDIT


class PolicyViolation(_CodedError):  # noqa: N818 - name fixed by the spec
    """An action was blocked by policy (for example, auto-closing a first PR)."""

    error_code = ErrorCode.POLICY_VIOLATION

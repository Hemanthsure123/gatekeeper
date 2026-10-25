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
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    INPUT_TOO_LARGE = "input_too_large"
    CAPABILITY_MISSING = "capability_missing"
    AUTHENTICATION_FAILED = "authentication_failed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    JOB_TIMEOUT = "job_timeout"
    IDENTITY_LEAK_BLOCKED = "identity_leak_blocked"
    GEMINI_KEYS_INVALID = "gemini_keys_invalid"
    PRIVATE_REPO_HOSTED_MODEL_BLOCKED = "private_repo_hosted_model_blocked"


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


class ModelUnavailable(_CodedError):  # noqa: N818 - name fixed by the spec
    """The model backend could not be reached or refused service. Retryable by default.

    ``retry_after`` carries a server-provided delay in seconds (HTTP Retry-After).
    """

    error_code = ErrorCode.MODEL_UNAVAILABLE
    default_retryable = True

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, retryable=retryable, details=details)
        self.retry_after = retry_after


class ModelTimeout(_CodedError):  # noqa: N818 - name fixed by the spec
    """A model call exceeded MODEL_TIMEOUT_S. Retryable."""

    error_code = ErrorCode.MODEL_TIMEOUT
    default_retryable = True


class ModelOutputInvalid(_CodedError):  # noqa: N818 - name fixed by the spec
    """Model output failed schema validation, including after the repair attempt."""

    error_code = ErrorCode.MODEL_OUTPUT_INVALID


class InputTooLarge(_CodedError):  # noqa: N818 - name fixed by the spec
    """The input does not fit the configured context window (never silently truncated)."""

    error_code = ErrorCode.INPUT_TOO_LARGE


class CapabilityMissing(_CodedError):  # noqa: N818 - name fixed by the spec
    """The request needs a feature (for example images) the backend or model lacks."""

    error_code = ErrorCode.CAPABILITY_MISSING


class AuthenticationFailed(_CodedError):  # noqa: N818 - matches the other error names
    """A request could not be authenticated (for example a bad webhook signature)."""

    error_code = ErrorCode.AUTHENTICATION_FAILED


class PayloadTooLarge(_CodedError):  # noqa: N818 - matches the other error names
    """A request body exceeded the configured limit."""

    error_code = ErrorCode.PAYLOAD_TOO_LARGE


class JobTimeout(_CodedError):  # noqa: N818 - matches the other error names
    """A job ran past JOB_TIMEOUT_S. Retryable: the queue backs off, then marks it dead."""

    error_code = ErrorCode.JOB_TIMEOUT
    default_retryable = True


class IdentityLeakBlocked(PolicyViolation):
    """The leak guard found a forbidden identity term in model input. Never retried."""

    error_code = ErrorCode.IDENTITY_LEAK_BLOCKED


class GeminiKeysInvalid(_CodedError):  # noqa: N818 - matches the other error names
    """Every key in the Gemini key pool was rejected. Not retryable: a person must act."""

    error_code = ErrorCode.GEMINI_KEYS_INVALID


class PrivateRepoHostedModelBlocked(PolicyViolation):
    """A private repository would be sent to a hosted model without explicit permission."""

    error_code = ErrorCode.PRIVATE_REPO_HOSTED_MODEL_BLOCKED

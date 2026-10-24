"""Tests for gatekeeper.errors."""

from __future__ import annotations

import pytest

from gatekeeper.errors import (
    AuditError,
    ConfigError,
    ErrorCode,
    ExternalServiceError,
    GatekeeperError,
    NotFoundError,
    PolicyViolation,
    ValidationFailed,
)


@pytest.mark.parametrize(
    ("cls", "code", "retryable"),
    [
        (ConfigError, "config_error", False),
        (ValidationFailed, "validation_failed", False),
        (NotFoundError, "not_found", False),
        (ExternalServiceError, "external_service_error", True),
        (AuditError, "audit_error", False),
        (PolicyViolation, "policy_violation", False),
    ],
)
def test_subclasses_have_stable_codes(
    cls: type[GatekeeperError], code: str, retryable: bool
) -> None:
    error = cls("something happened")  # type: ignore[call-arg]
    assert isinstance(error, GatekeeperError)
    assert error.code == code
    assert error.retryable is retryable
    assert str(error) == "something happened"


def test_codes_are_unique() -> None:
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))


def test_public_dict_excludes_details_and_cause() -> None:
    try:
        try:
            raise RuntimeError("internal db host 10.0.0.5 exploded")
        except RuntimeError as cause:
            raise ExternalServiceError(
                "GitHub is unavailable", details={"status": 503, "body": "internal"}
            ) from cause
    except ExternalServiceError as error:
        public = error.to_public_dict()

    assert public == {
        "code": "external_service_error",
        "message": "GitHub is unavailable",
        "retryable": True,
    }
    assert "10.0.0.5" not in repr(public)
    assert "details" not in public


def test_retryable_can_be_overridden() -> None:
    assert ExternalServiceError("bad request", retryable=False).retryable is False
    assert GatekeeperError("custom_code", "msg", retryable=True).code == "custom_code"


def test_details_are_kept_internally() -> None:
    error = AuditError("write failed", details={"run_id": "r1"})
    assert error.details == {"run_id": "r1"}
    assert "AuditError" in repr(error)

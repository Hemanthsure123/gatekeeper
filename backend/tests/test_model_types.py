"""Tests for gatekeeper.models.types and the model error classes."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from gatekeeper.errors import (
    CapabilityMissing,
    InputTooLarge,
    ModelOutputInvalid,
    ModelTimeout,
    ModelUnavailable,
)
from gatekeeper.models.types import (
    DEFAULT_SEED,
    CachePolicy,
    ImagePart,
    ModelRequest,
    ModelRole,
    Reasoning,
    TextPart,
)


class Out(BaseModel):
    ok: bool


def request(**overrides: object) -> ModelRequest[Out]:
    values: dict[str, object] = {
        "prompt_id": "test.prompt",
        "prompt_version": "1",
        "parts": [TextPart(text="hi")],
        "response_model": Out,
    }
    values.update(overrides)
    return ModelRequest[Out].model_validate(values)


def test_defaults() -> None:
    req = request()
    assert req.role is ModelRole.PRIMARY
    assert req.temperature == 0
    assert req.seed == DEFAULT_SEED
    assert req.reasoning is Reasoning.OFF
    assert req.cache_policy is CachePolicy.USE
    assert req.text_parts == ["hi"]
    assert req.images == []


def test_request_is_immutable() -> None:
    req = request()
    with pytest.raises(ValidationError):
        req.temperature = 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt_id": ""},
        {"prompt_version": "   "},
        {"parts": []},
        {"temperature": 3},
        {"max_output_tokens": 0},
        {"response_model": dict},
    ],
)
def test_rejects_invalid_requests(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


def test_image_part_hash_and_repr() -> None:
    image = ImagePart.from_bytes(b"\x89PNG-bytes", "image/png")
    assert len(image.sha256) == 64
    assert "PNG-bytes" not in repr(image)
    with pytest.raises(ValidationError, match="sha256"):
        ImagePart(mime_type="image/png", data=b"x", sha256="0" * 64)
    req = request(parts=[TextPart(text="a"), image])
    assert req.images == [image]


@pytest.mark.parametrize(
    ("cls", "code", "retryable"),
    [
        (ModelUnavailable, "model_unavailable", True),
        (ModelTimeout, "model_timeout", True),
        (ModelOutputInvalid, "model_output_invalid", False),
        (InputTooLarge, "input_too_large", False),
        (CapabilityMissing, "capability_missing", False),
    ],
)
def test_model_error_codes(cls: type[ModelUnavailable], code: str, retryable: bool) -> None:
    error = cls("x")
    assert (error.code, error.retryable) == (code, retryable)


def test_model_unavailable_carries_retry_after() -> None:
    assert ModelUnavailable("busy", retry_after=3.0).retry_after == 3.0

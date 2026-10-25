"""Typed requests and responses for model calls."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

#: Fixed default seed so identical requests are as reproducible as the backend allows.
DEFAULT_SEED: Final = 42

ImageMime = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelRole(StrEnum):
    """PRIMARY -> OLLAMA_MODEL / GEMMA_MODEL_PRIMARY; SECONDARY -> *_SECONDARY."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class Reasoning(StrEnum):
    OFF = "off"
    ON = "on"


class CachePolicy(StrEnum):
    USE = "use"  # read and write
    REFRESH = "refresh"  # write only
    BYPASS = "bypass"  # neither


class TextPart(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """Image bytes plus their SHA-256. Audit events and cache keys only ever see the hash."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["image"] = "image"
    mime_type: ImageMime
    data: bytes = Field(repr=False)
    sha256: str

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: ImageMime) -> ImagePart:
        return cls(mime_type=mime_type, data=data, sha256=hashlib.sha256(data).hexdigest())

    @model_validator(mode="after")
    def _hash_matches(self) -> ImagePart:
        if hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise ValueError("sha256 does not match the image bytes")
        return self


Part = Annotated[TextPart | ImagePart, Field(discriminator="kind")]


class ModelRequest[T: BaseModel](BaseModel):
    """One model call. Immutable; ``response_model`` defines and validates the output."""

    model_config = ConfigDict(frozen=True)

    prompt_id: NonEmpty
    prompt_version: NonEmpty
    role: ModelRole = ModelRole.PRIMARY
    system: str = ""
    parts: tuple[Part, ...] = Field(min_length=1)
    response_model: type[T]
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int = DEFAULT_SEED
    max_output_tokens: int = Field(default=1024, ge=1, le=65_536)
    reasoning: Reasoning = Reasoning.OFF
    cache_policy: CachePolicy = CachePolicy.USE

    @property
    def text_parts(self) -> list[str]:
        return [part.text for part in self.parts if isinstance(part, TextPart)]

    @property
    def images(self) -> list[ImagePart]:
        return [part for part in self.parts if isinstance(part, ImagePart)]


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse[T: BaseModel]:
    parsed: T
    raw_text: str
    model: str
    backend: str
    usage: Usage
    latency_ms: float
    cache_hit: bool
    attempts: int
    #: Backend facts safe to audit (e.g. the Gemini key alias and failovers); no secrets.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendHealth:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    supports_images: bool
    supports_structured_output: bool
    supports_reasoning_control: bool
    #: Context window the adapter enforces (for Ollama: OLLAMA_NUM_CTX, capped by the model).
    max_context_tokens: int | None = None

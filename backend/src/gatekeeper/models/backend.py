"""Interfaces.

``ModelBackend`` is what every feature calls. It is implemented once, by
:class:`gatekeeper.models.adapter.ModelAdapter`, which wraps a ``BackendDriver``
(Ollama, Gemini, or the test fake) and adds validation, retries, caching, audit and
logging. Drivers only translate one call to their wire format and back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from gatekeeper.audit import Run
from gatekeeper.models.types import (
    BackendHealth,
    Capabilities,
    ImagePart,
    ModelRequest,
    ModelResponse,
    ModelRole,
    Reasoning,
    TextPart,
)


class ModelBackend(Protocol):
    def generate[T: BaseModel](
        self, request: ModelRequest[T], run: Run | None = None, *, allow_unaudited: bool = False
    ) -> ModelResponse[T]: ...

    def health(self) -> BackendHealth: ...

    def capabilities(self) -> Capabilities: ...


@dataclass(frozen=True)
class Turn:
    """An extra conversation turn after the user content (used by the repair attempt)."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class BackendCall:
    """Everything a driver needs for one HTTP/API call."""

    model: str
    system: str
    parts: tuple[TextPart | ImagePart, ...]
    json_schema: dict[str, Any]
    temperature: float
    seed: int
    max_output_tokens: int
    reasoning: Reasoning
    followups: tuple[Turn, ...] = ()


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


class BackendDriver(Protocol):
    """One wire protocol. Raises ModelUnavailable/ModelTimeout (retryable) or other
    GatekeeperErrors (not retryable); never retries itself."""

    @property
    def name(self) -> str: ...

    def model_for(self, role: ModelRole) -> str: ...

    def capabilities(self) -> Capabilities: ...

    def health(self) -> BackendHealth: ...

    def complete(self, call: BackendCall) -> Completion: ...

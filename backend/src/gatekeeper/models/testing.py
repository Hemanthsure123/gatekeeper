"""Test doubles for the model layer. Every later test should use these, never a live model.

Typical use::

    adapter, fake = fake_adapter([Verdict(label="ok")])
    response = adapter.generate(request, allow_unaudited=True)
    assert fake.calls[0].model == "fake-primary"
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from gatekeeper.errors import ConfigError
from gatekeeper.models.adapter import ModelAdapter, RequestValidator
from gatekeeper.models.backend import BackendCall, Completion
from gatekeeper.models.cache import ModelCache
from gatekeeper.models.types import BackendHealth, Capabilities, ModelRole

#: A scripted reply: model instance / dict (serialized to JSON), raw text, a full
#: Completion, an exception to raise, or a callable computing any of those from the call.
Scripted = BaseModel | Mapping[str, Any] | str | Completion | BaseException
Responder = Callable[[BackendCall], Scripted]

DEFAULT_CAPABILITIES = Capabilities(
    supports_images=True,
    supports_structured_output=True,
    supports_reasoning_control=True,
    max_context_tokens=8192,
)


class FakeBackend:
    """Scripted ``BackendDriver``. Records every call in ``calls``."""

    def __init__(
        self,
        responses: Iterable[Scripted | Responder] | Responder = (),
        *,
        name: str = "fake",
        models: Mapping[ModelRole, str] | None = None,
        capabilities: Capabilities = DEFAULT_CAPABILITIES,
        health: BackendHealth | None = None,
    ) -> None:
        self._responder: Responder | None = responses if callable(responses) else None
        self._queue: deque[Scripted | Responder] = (
            deque() if callable(responses) else deque(responses)
        )
        self._name = name
        self._models = dict(
            models
            if models is not None
            else {ModelRole.PRIMARY: "fake-primary", ModelRole.SECONDARY: "fake-secondary"}
        )
        self._capabilities = capabilities
        self._health = health or BackendHealth(ok=True, message="fake backend ready")
        self.calls: list[BackendCall] = []

    @property
    def name(self) -> str:
        return self._name

    def model_for(self, role: ModelRole) -> str:
        try:
            return self._models[role]
        except KeyError:
            raise ConfigError(f"no {role.value} model configured for the fake backend") from None

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def health(self) -> BackendHealth:
        return self._health

    def push(self, *responses: Scripted | Responder) -> None:
        self._queue.extend(responses)

    def complete(self, call: BackendCall) -> Completion:
        self.calls.append(call)
        if self._responder is not None:
            item: Scripted | Responder = self._responder
        elif self._queue:
            item = self._queue.popleft()
        else:
            raise AssertionError("FakeBackend has no scripted response left")
        result = item(call) if callable(item) else item
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, Completion):
            return result
        if isinstance(result, BaseModel):
            text = result.model_dump_json()
        elif isinstance(result, Mapping):
            text = json.dumps(result)
        else:
            text = result
        return Completion(text=text, input_tokens=None, output_tokens=None, finish_reason="stop")


def fake_adapter(
    responses: Iterable[Scripted | Responder] | Responder = (),
    *,
    cache: ModelCache | None = None,
    validators: Iterable[RequestValidator] = (),
    max_retries: int = 2,
    **backend_options: Any,
) -> tuple[ModelAdapter, FakeBackend]:
    """A real ModelAdapter over a FakeBackend, with sleeping disabled."""
    backend = FakeBackend(responses, **backend_options)
    adapter = ModelAdapter(
        backend,
        max_retries=max_retries,
        cache=cache,
        validators=validators,
        sleep=lambda _seconds: None,
    )
    return adapter, backend

"""The identity leak guard on the model adapter."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from gatekeeper.errors import IdentityLeakBlocked, PolicyViolation
from gatekeeper.models.guards import forbidden_terms_guard
from gatekeeper.models.testing import fake_adapter
from gatekeeper.models.types import ModelRequest, TextPart


class Out(BaseModel):
    ok: bool


def request(text: str, system: str = "") -> ModelRequest[Out]:
    return ModelRequest[Out](
        prompt_id="t",
        prompt_version="1",
        system=system,
        parts=(TextPart(text=text),),
        response_model=Out,
    )


@pytest.mark.parametrize(
    ("text", "system"),
    [("change by synth-dev", ""), ("SYNTH-DEV did this", ""), ("fine", "note for Synth-Dev")],
)
def test_rejects_forbidden_terms_before_any_backend_call(text: str, system: str) -> None:
    adapter, fake = fake_adapter([Out(ok=True)])
    guarded = adapter.with_validators(forbidden_terms_guard({"synth-dev"}))
    with pytest.raises(PolicyViolation) as caught:
        guarded.generate(request(text, system), allow_unaudited=True)
    assert fake.calls == []
    assert "synth-dev" not in caught.value.message.lower()
    assert isinstance(caught.value, IdentityLeakBlocked)
    assert (caught.value.code, caught.value.retryable) == ("identity_leak_blocked", False)


def test_allows_clean_text_and_partial_words() -> None:
    adapter, fake = fake_adapter([Out(ok=True), Out(ok=True)])
    guarded = adapter.with_validators(forbidden_terms_guard({"synth-dev"}))
    assert guarded.generate(
        request("synth-developer and xsynth-dev"), allow_unaudited=True
    ).parsed.ok
    assert len(fake.calls) == 1


def test_with_validators_does_not_change_the_original_adapter() -> None:
    adapter, fake = fake_adapter([Out(ok=True)])
    adapter.with_validators(forbidden_terms_guard({"synth-dev"}))
    assert adapter.generate(request("by synth-dev"), allow_unaudited=True).parsed.ok
    assert len(fake.calls) == 1


def test_empty_terms_allow_everything() -> None:
    adapter, _ = fake_adapter([Out(ok=True)])
    guarded = adapter.with_validators(forbidden_terms_guard({" ", ""}))
    assert guarded.generate(request("anything"), allow_unaudited=True).parsed.ok

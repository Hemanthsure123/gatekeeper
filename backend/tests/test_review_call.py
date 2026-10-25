"""The review prompt, schema and the single model call (FakeBackend, fake audit run)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from pydantic import ValidationError

from factories import item_content
from gatekeeper.audit import Actor, EventType, Run
from gatekeeper.config import REVIEW_PROMPT_OVERHEAD_TOKENS, Settings
from gatekeeper.errors import InputTooLarge, PolicyViolation
from gatekeeper.models.adapter import compose_system, estimate_tokens
from gatekeeper.models.backend import BackendCall
from gatekeeper.models.testing import fake_adapter
from gatekeeper.prompts import UNTRUSTED_CONTENT_RULE
from gatekeeper.review_core.diff import build_diff_view
from gatekeeper.review_core.review import REVIEW_PR, review_result_model, run_review
from gatekeeper.review_core.signals import SignalInput, compute_signals, signal_summary

GOOD: dict[str, Any] = {
    "verdict": "looks_valuable",
    "value_score": 0.8,
    "claim_match": "matches",
    "rationale": "Clear improvement to error messages.",
    "evidence": [
        {
            "path": "src/app.py",
            "line_start": 1,
            "line_end": 1,
            "quote": "new",
            "claim": "Replaces the old value.",
            "polarity": "supports_value",
        }
    ],
    "contributor_note": "Thanks, this reads well.",
    "self_confidence": 0.7,
}


@dataclass
class FakeEvent:
    seq: int


@dataclass
class FakeRun:
    events: list[tuple[EventType, Actor, dict[str, Any]]] = field(default_factory=list)

    def record(
        self,
        event_type: EventType,
        actor: Actor,
        payload: dict[str, Any],
        parent_seq: int | None = None,
    ) -> FakeEvent:
        self.events.append((event_type, actor, payload))
        return FakeEvent(len(self.events))


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def review(responses: list[Any], *, terms: frozenset[str] = frozenset(), **content: Any) -> Any:
    adapter, fake = fake_adapter(responses)
    item = item_content(**content)
    signals = compute_signals(SignalInput(item))
    run = FakeRun()
    outcome = run_review(
        adapter,
        content=item,
        diff=build_diff_view(item, 5_000),
        signals=signals,
        run=cast(Run, run),
        forbidden_terms=terms,
        settings=settings(),
    )
    return outcome, fake, run


def test_prompt_rules_and_untrusted_blocks() -> None:
    assert UNTRUSTED_CONTENT_RULE in REVIEW_PR.system
    for rule in ("Do not infer anything about the author", "Cite only lines", "neutral"):
        assert rule in REVIEW_PR.system
    assert REVIEW_PR.untrusted_slots == {"title", "body", "signals", "diff"}
    assert (REVIEW_PR.id, REVIEW_PR.version) == ("review.pr", "1")


def test_prompt_overhead_fits_the_reserved_budget() -> None:
    schema = review_result_model(5).model_json_schema()
    rendered = REVIEW_PR.render(
        title="",
        body="",
        signals=signal_summary(compute_signals(SignalInput(item_content()))),
        diff="",
    )
    estimate = estimate_tokens([compose_system(rendered.system, schema), rendered.user])
    assert estimate < REVIEW_PROMPT_OVERHEAD_TOKENS


def test_schema_caps_evidence_and_lengths() -> None:
    model = review_result_model(2)
    assert model.model_json_schema()["properties"]["evidence"]["maxItems"] == 2
    with pytest.raises(ValidationError):
        model.model_validate({**GOOD, "evidence": GOOD["evidence"] * 3})
    with pytest.raises(ValidationError):
        model.model_validate({**GOOD, "rationale": "x" * 601})
    with pytest.raises(ValidationError):
        model.model_validate({**GOOD, "verdict": "spam"})


def test_successful_call_is_audited_by_the_adapter() -> None:
    outcome, fake, run = review([GOOD])
    assert outcome.result is not None
    assert outcome.result.verdict == "looks_valuable"
    assert outcome.abstain_reason is None
    (call,) = fake.calls
    assert isinstance(call, BackendCall)
    assert (call.temperature, call.max_output_tokens, call.reasoning.value) == (0.0, 800, "off")
    assert '<untrusted_content name="diff">' in call.parts[0].text  # type: ignore[union-attr]
    assert [e[0] for e in run.events] == [EventType.MODEL_REQUEST, EventType.MODEL_RESPONSE]
    assert run.events[0][2]["prompt_id"] == "review.pr"


def test_invalid_output_abstains_instead_of_failing() -> None:
    outcome, fake, _ = review(["not json", '{"verdict": "looks_valuable"}'])
    assert outcome.result is None
    assert outcome.abstain_reason == "model_output_invalid"
    assert len(fake.calls) == 2  # the adapter's single repair attempt


def test_input_too_large_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_large(*_args: object, **_kwargs: object) -> None:
        raise InputTooLarge("too big")

    from gatekeeper.models import adapter as adapter_module

    monkeypatch.setattr(adapter_module.ModelAdapter, "generate", too_large)
    outcome, _, _ = review([GOOD])
    assert outcome.abstain_reason == "input_too_large"


def test_leak_guard_blocks_forbidden_terms() -> None:
    with pytest.raises(PolicyViolation):
        review([GOOD], terms=frozenset({"leaky"}), body="written by leaky yesterday")

"""Confidence v1 monotonicity and the abstain thresholds."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from gatekeeper.config import Settings
from gatekeeper.review_core.confidence import confidence, decide, signal_agreement
from gatekeeper.review_core.signals import SignalResult

STEPS = [0.0, 0.25, 0.5, 0.75, 1.0]


def base(**overrides: Any) -> float:
    values: dict[str, Any] = {
        "self_confidence": 0.8,
        "citation_validity": 1.0,
        "agreement": 1.0,
        "truncated": False,
        "looks_valuable_without_evidence": False,
    }
    values.update(overrides)
    return confidence(**values)


def sig(direction: str, severity: str = "medium") -> SignalResult:
    return SignalResult(id="s", value=1, direction=direction, severity=severity, explanation="e")  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["self_confidence", "citation_validity", "agreement"])
def test_monotone_in_each_input(field: str) -> None:
    for others in itertools.product(STEPS, repeat=2):
        fixed = dict(
            zip(
                [f for f in ("self_confidence", "citation_validity", "agreement") if f != field],
                others,
                strict=True,
            )
        )
        values = [base(**fixed, **{field: step}) for step in STEPS]
        assert values == sorted(values)


def test_truncation_and_missing_evidence_only_lower_it() -> None:
    assert base(truncated=True) < base()
    assert base(looks_valuable_without_evidence=True) < base()
    assert 0.0 <= base(self_confidence=1.0) <= 1.0


def test_signal_agreement() -> None:
    positive = [sig("positive", "high")]
    negative = [sig("negative", "high")]
    assert signal_agreement("looks_valuable", positive) == 1.0
    assert signal_agreement("looks_valuable", negative) == 0.0
    assert signal_agreement("likely_low_value", negative) == 1.0
    assert signal_agreement("looks_valuable", []) == 0.5
    assert signal_agreement("needs_human_look", positive + negative) == 1.0
    assert signal_agreement("needs_human_look", positive) == 0.5


def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("verdict", "value", "truncated", "expected"),
    [
        ("looks_valuable", 0.9, False, ("looks_valuable", "completed", None)),
        ("looks_valuable", 0.59, False, ("needs_human_look", "abstained", "low_confidence")),
        ("looks_valuable", 0.6, False, ("looks_valuable", "completed", None)),
        ("looks_valuable", 0.99, True, ("needs_human_look", "abstained", "truncated_input")),
        (
            "likely_low_value",
            0.7,
            False,
            ("needs_human_look", "abstained", "negative_needs_more_confidence"),
        ),
        ("likely_low_value", 0.75, False, ("likely_low_value", "completed", None)),
        ("needs_human_look", 0.8, False, ("needs_human_look", "completed", None)),
    ],
)
def test_decide(verdict: Any, value: float, truncated: bool, expected: tuple[Any, ...]) -> None:
    decision = decide(verdict, value, truncated=truncated, settings=settings())
    assert (decision.verdict, decision.status, decision.abstain_reason) == expected

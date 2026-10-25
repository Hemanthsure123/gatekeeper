"""Confidence v1 and the final decision.

THIS IS A HEURISTIC, NOT A CALIBRATED PROBABILITY. Later work fits it to labelled data;
until then, read it as an ordering ("more" vs "less" sure), not as a percentage.

    confidence = self_confidence
               x (0.5 + 0.5 x citation_validity)
               x (0.6 + 0.4 x signal_agreement)
               x completeness                 (1.0 complete, 0.5 truncated)
               x evidence_factor              (0.8 for looks_valuable with no verified
                                               evidence, else 1.0)

Every factor is non-decreasing in its input, so the result is monotone: more
self-confidence, more valid citations, more agreement or a complete input never lower
it. The result is clamped to [0, 1].

signal_agreement (0..1) compares the verdict's direction (looks_valuable +1,
likely_low_value -1, needs_human_look 0) with the net direction of the non-neutral
signals, weighted by severity (low 1, medium 2, high 3). With no non-neutral signals it
is 0.5 (no information either way).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from gatekeeper.config import Settings
from gatekeeper.review_core.review import VerdictLabel
from gatekeeper.review_core.signals import SEVERITY_WEIGHT, SignalResult

TRUNCATED_COMPLETENESS: Final = 0.5
NO_EVIDENCE_FACTOR: Final = 0.8
VERDICT_DIRECTION: Final = {
    "looks_valuable": 1.0,
    "needs_human_look": 0.0,
    "likely_low_value": -1.0,
}


def net_signal_direction(signals: Sequence[SignalResult]) -> float | None:
    """Severity-weighted mean direction of non-neutral signals in [-1, 1], or None."""
    total = score = 0.0
    for signal in signals:
        if signal.direction == "neutral":
            continue
        weight = SEVERITY_WEIGHT[signal.severity]
        total += weight
        score += weight if signal.direction == "positive" else -weight
    return score / total if total else None


def signal_agreement(verdict: VerdictLabel, signals: Sequence[SignalResult]) -> float:
    net = net_signal_direction(signals)
    if net is None:
        return 0.5
    direction = VERDICT_DIRECTION[verdict]
    if direction == 0.0:
        return 1.0 - abs(net) / 2  # an unsure verdict agrees with mixed signals
    return (1.0 + direction * net) / 2


def confidence(
    *,
    self_confidence: float,
    citation_validity: float,
    agreement: float,
    truncated: bool,
    looks_valuable_without_evidence: bool,
) -> float:
    value = (
        self_confidence
        * (0.5 + 0.5 * citation_validity)
        * (0.6 + 0.4 * agreement)
        * (TRUNCATED_COMPLETENESS if truncated else 1.0)
        * (NO_EVIDENCE_FACTOR if looks_valuable_without_evidence else 1.0)
    )
    return round(min(1.0, max(0.0, value)), 4)


@dataclass(frozen=True)
class Decision:
    verdict: VerdictLabel
    status: str  # "completed" or "abstained"
    abstain_reason: str | None


def decide(verdict: VerdictLabel, value: float, *, truncated: bool, settings: Settings) -> Decision:
    """Abstain (final verdict needs_human_look) when unsure, truncated, or when a negative
    verdict lacks the higher confidence it requires."""
    reason = None
    if truncated:
        reason = "truncated_input"
    elif value < settings.review_abstain_threshold:
        reason = "low_confidence"
    elif verdict == "likely_low_value" and value < settings.review_negative_min_confidence:
        reason = "negative_needs_more_confidence"
    if reason is not None:
        return Decision("needs_human_look", "abstained", reason)
    return Decision(verdict, "completed", None)

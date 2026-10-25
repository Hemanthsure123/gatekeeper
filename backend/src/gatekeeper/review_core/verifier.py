"""Verify every citation with code. No model calls.

An evidence item is kept only if:
- its path was shown to the model with a patch;
- ``line_start <= line_end``, the span is at most ``MAX_SPAN_LINES`` lines, and every
  cited line is an added or context line on the new side;
- its quote, after whitespace normalisation, appears in the cited lines.

Safety rule: a ``likely_low_value`` verdict needs at least one verified
``raises_concern`` item or one high-severity negative signal. Otherwise it becomes
``needs_human_look``: the system never labels work low value without proof.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from gatekeeper.review_core.diff import DiffView
from gatekeeper.review_core.review import Evidence, ReviewResult, VerdictLabel
from gatekeeper.review_core.signals import SignalResult

MAX_SPAN_LINES: Final = 40
_WS = re.compile(r"\s+")
#: Quotes sometimes repeat the rendering prefix ("L12| +code"); strip it per line.
_RENDER_PREFIX = re.compile(r"^\s*(?:L\d+\|\s?[+ ]?|[+ ](?=\S))")


@dataclass(frozen=True)
class DroppedEvidence:
    index: int
    path: str
    reason: str


@dataclass(frozen=True)
class Verification:
    kept: tuple[Evidence, ...]
    dropped: tuple[DroppedEvidence, ...]
    citation_validity: float
    verdict: VerdictLabel
    downgrade_reason: str | None = None


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _quote_text(quote: str) -> str:
    return normalize(" ".join(_RENDER_PREFIX.sub("", line) for line in quote.splitlines()))


def check_evidence(item: Evidence, diff: DiffView) -> str | None:
    """None if the citation is valid, else a short reason code."""
    lines = diff.citable.get(item.path)
    if lines is None:
        return (
            "no_patch"
            if item.path in diff.without_patch or item.path in diff.omitted
            else "unknown_path"
        )
    if item.line_start > item.line_end:
        return "bad_range"
    if item.line_end - item.line_start + 1 > MAX_SPAN_LINES:
        return "span_too_long"
    cited = [lines.get(number) for number in range(item.line_start, item.line_end + 1)]
    if any(text is None for text in cited):
        return "line_not_in_diff"
    quote = _quote_text(item.quote)
    if not quote or quote not in normalize(" ".join(text or "" for text in cited)):
        return "quote_not_found"
    return None


def verify(result: ReviewResult, diff: DiffView, signals: Sequence[SignalResult]) -> Verification:
    kept: list[Evidence] = []
    dropped: list[DroppedEvidence] = []
    for index, item in enumerate(result.evidence):
        reason = check_evidence(item, diff)
        if reason is None:
            kept.append(item)
        else:
            dropped.append(DroppedEvidence(index, item.path, reason))
    total = len(result.evidence)
    validity = len(kept) / total if total else 1.0

    verdict: VerdictLabel = result.verdict
    downgrade = None
    if verdict == "likely_low_value":
        verified_concern = any(item.polarity == "raises_concern" for item in kept)
        strong_signal = any(s.direction == "negative" and s.severity == "high" for s in signals)
        if not (verified_concern or strong_signal):
            verdict = "needs_human_look"
            downgrade = "no_verified_concern"
    return Verification(tuple(kept), tuple(dropped), validity, verdict, downgrade)

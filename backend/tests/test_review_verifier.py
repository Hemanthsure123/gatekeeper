"""Citation verification and the no-accusation-without-proof rule."""

from __future__ import annotations

from typing import Any

import pytest

from factories import file_change, item_content
from gatekeeper.review_core.diff import build_diff_view
from gatekeeper.review_core.review import Evidence, ReviewResult
from gatekeeper.review_core.signals import SignalResult
from gatekeeper.review_core.verifier import check_evidence, verify

PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " def load(path):\n"
    "-    return open(path)\n"
    "+    with open(path) as f:\n"
    "+        return f.read()\n"
    " # end"
)
VIEW = build_diff_view(
    item_content(
        [file_change("src/io.py", patch=PATCH), file_change("img.png", patch=None, binary=True)]
    ),
    5_000,
)


def evidence(**overrides: Any) -> Evidence:
    values: dict[str, Any] = {
        "path": "src/io.py",
        "line_start": 2,
        "line_end": 3,
        "quote": "with open(path) as f:",
        "claim": "Closes the file.",
        "polarity": "supports_value",
    }
    values.update(overrides)
    return Evidence(**values)


def result(verdict: str = "looks_valuable", items: list[Evidence] | None = None) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,  # type: ignore[arg-type]
        value_score=0.5,
        claim_match="matches",
        rationale="r",
        evidence=items if items is not None else [evidence()],
        contributor_note="",
        self_confidence=0.5,
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"quote": "L2| +    with   open(path) as f:"}, None),
        ({"line_start": 1, "line_end": 4, "quote": "def load(path): with open(path) as f:"}, None),
        ({"path": "src/other.py"}, "unknown_path"),
        ({"path": "img.png"}, "no_patch"),
        ({"line_start": 3, "line_end": 2}, "bad_range"),
        ({"line_start": 1, "line_end": 41}, "span_too_long"),
        ({"line_start": 4, "line_end": 6}, "line_not_in_diff"),
        ({"quote": "return open(path)", "line_start": 2, "line_end": 2}, "quote_not_found"),
        ({"quote": "import os"}, "quote_not_found"),
    ],
)
def test_check_evidence(overrides: dict[str, Any], reason: str | None) -> None:
    assert check_evidence(evidence(**overrides), VIEW) == reason


def test_verify_drops_bad_items_and_computes_validity() -> None:
    items = [evidence(), evidence(path="nope.py"), evidence(quote="fabricated")]
    verification = verify(result(items=items), VIEW, [])
    assert verification.kept == (items[0],)
    assert [(d.index, d.reason) for d in verification.dropped] == [
        (1, "unknown_path"),
        (2, "quote_not_found"),
    ]
    assert verification.citation_validity == pytest.approx(1 / 3)
    assert verify(result(items=[]), VIEW, []).citation_validity == 1.0


def test_negative_verdict_without_proof_is_downgraded() -> None:
    unproven = verify(
        result("likely_low_value", [evidence(quote="fabricated", polarity="raises_concern")]),
        VIEW,
        [],
    )
    assert (unproven.verdict, unproven.downgrade_reason) == (
        "needs_human_look",
        "no_verified_concern",
    )


def test_negative_verdict_with_verified_concern_or_strong_signal_is_kept() -> None:
    proven = verify(result("likely_low_value", [evidence(polarity="raises_concern")]), VIEW, [])
    assert (proven.verdict, proven.downgrade_reason) == ("likely_low_value", None)
    strong = SignalResult(
        id="whitespace_only", value=1.0, direction="negative", severity="high", explanation="x"
    )
    by_signal = verify(result("likely_low_value", []), VIEW, [strong])
    assert by_signal.verdict == "likely_low_value"

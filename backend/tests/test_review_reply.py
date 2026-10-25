"""The deterministic reply composer and its banned-word filter."""

from __future__ import annotations

from typing import Any

import pytest

from gatekeeper.review_core.reply import (
    ABSTAIN_LINE,
    BANNED_WORDS,
    NEUTRAL_NOTE,
    SUGGESTIONS,
    SUMMARY,
    compose_reply,
    contains_banned,
)
from gatekeeper.review_core.review import Evidence
from gatekeeper.review_core.signals import SignalResult


def evidence(claim: str = "Adds input validation.", start: int = 3, end: int = 5) -> Evidence:
    return Evidence(
        path="src/a.py",
        line_start=start,
        line_end=end,
        quote="x",
        claim=claim,
        polarity="supports_value",
    )


def sig(id_: str, direction: str) -> SignalResult:
    return SignalResult(id=id_, value=0, direction=direction, severity="low", explanation="e")  # type: ignore[arg-type]


def reply(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "verdict": "looks_valuable",
        "abstained": False,
        "evidence": [evidence()],
        "signals": [],
        "contributor_note": "Nice, clear change.",
    }
    values.update(overrides)
    return compose_reply(**values)


def test_completed_reply_structure() -> None:
    text = reply(evidence=[evidence(), evidence(start=7, end=7), evidence(), evidence()])
    assert text.startswith("**Looks valuable.**")
    assert "- `src/a.py:L3-L5`: Adds input validation." in text
    assert "- `src/a.py:L7`: Adds input validation." in text
    assert text.count("\n- `") == 3
    assert text.endswith("Nice, clear change.")


def test_fixed_summary_wording() -> None:
    assert set(SUMMARY.values()) == {"Looks valuable", "Needs a human look", "Likely low value"}
    assert reply(verdict="likely_low_value").startswith("**Likely low value.**")


def test_abstained_reply_says_a_maintainer_will_look() -> None:
    text = reply(abstained=True, verdict="needs_human_look")
    assert text.startswith("**Needs a human look.**")
    assert ABSTAIN_LINE in text


def test_suggestions_come_from_signals() -> None:
    signals = [
        sig("tests_touched", "negative"),
        sig("linked_issue", "neutral"),
        sig("ci_status", "positive"),
        sig("whitespace_only", "negative"),
    ]
    text = reply(signals=signals)
    assert SUGGESTIONS["tests_touched"] in text
    assert SUGGESTIONS["linked_issue"] in text
    assert SUGGESTIONS["whitespace_only"] in text
    assert SUGGESTIONS["ci_status"] not in text


@pytest.mark.parametrize(
    "note",
    [
        "This is spam.",
        "Looks AI-generated to me",
        "lazy work",
        "Junk PR",
        "probably a BOT",
        "ai  generated",
    ],
)
def test_banned_words_replace_the_model_note(note: str) -> None:
    text = reply(contributor_note=note)
    assert note not in text
    assert text.endswith(NEUTRAL_NOTE)
    assert not contains_banned(text)


def test_only_the_model_note_is_filtered() -> None:
    claim = "Fixes the reply command of the chat bot."
    text = reply(evidence=[evidence(claim), evidence("Adds a test.")], contributor_note="")
    assert claim in text
    assert "Adds a test." in text


def test_mentions_are_defused_and_ordinary_words_pass() -> None:
    text = reply(contributor_note="Thanks @someone, the robot arm docs read well.")
    assert "@someone" not in text
    assert "robot arm" in text
    assert not contains_banned("robotics and botany")


def test_fixed_strings_are_clean() -> None:
    for text in [*SUMMARY.values(), ABSTAIN_LINE, NEUTRAL_NOTE, *SUGGESTIONS.values()]:
        assert not contains_banned(text)
    assert "spam" in BANNED_WORDS

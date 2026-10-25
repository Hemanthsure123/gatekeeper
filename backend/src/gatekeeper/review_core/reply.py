"""Deterministic reply draft (Markdown). No model call. Stored, never posted.

Structure: a fixed neutral summary line, up to three verified evidence bullets with
``path:lines``, concrete suggestions derived from signals, and the model's contributor
note if it passes the safety filter. Abstained results say a maintainer will take a look.

Safety filter: every contributor-facing string is checked against ``BANNED_WORDS``
(words that accuse or label people). A hit drops that string; a hit in the model's note
replaces it with a neutral template. ``@mentions`` are defused so a later post never
pings anyone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from gatekeeper.review_core.review import Evidence, VerdictLabel
from gatekeeper.review_core.signals import SignalResult

SUMMARY: Final = {
    "looks_valuable": "Looks valuable",
    "needs_human_look": "Needs a human look",
    "likely_low_value": "Likely low value",
}
ABSTAIN_LINE: Final = "Thanks for the contribution. A maintainer will take a look."
NEUTRAL_NOTE: Final = "Thank you for the contribution."
MAX_BULLETS: Final = 3
MAX_NOTE_CHARS: Final = 500

#: Words that accuse or label people. Keep this the single list (CLAUDE.md).
BANNED_WORDS: Final = frozenset(
    {
        "spam", "spammer", "spammy", "spamming", "bot", "bots", "ai-generated",
        "ai generated", "chatgpt", "llm-generated", "machine-generated", "fake", "fraud",
        "scam", "lazy", "garbage", "junk", "trash", "rubbish", "worthless", "useless",
        "low-effort", "low effort", "stupid", "dumb", "idiot", "idiotic", "clueless",
        "incompetent", "sloppy", "troll", "farming", "cheater", "cheating",
    }
)  # fmt: skip
_BANNED = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(
        re.escape(w).replace(r"\ ", r"[\s-]+") for w in sorted(BANNED_WORDS, key=len, reverse=True)
    )
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MENTION = re.compile(r"@(?=[A-Za-z0-9\[])")

SUGGESTIONS: Final = {
    "tests_touched": "Consider adding or updating tests for the changed code.",
    "linked_issue": (
        'If this addresses an issue, link it in the description (for example "Fixes #123").'
    ),
    "description_present": "Please add a short description of what the change does and why.",
    "whitespace_only": (
        "Please keep whitespace-only or unrelated edits out of this change so it is easy "
        "to review."
    ),
    "ci_status": "Some CI checks are failing; please take a look at them.",
    "tiny_change": "Please explain how this small change relates to the description.",
}  # fmt: skip


def contains_banned(text: str) -> bool:
    return _BANNED.search(text) is not None


def _safe(text: str) -> str | None:
    """The text with mentions defused, or None if it trips the banned-word filter."""
    text = text.strip()
    if not text or contains_banned(text):
        return None
    return _MENTION.sub("@​", text)


def suggestions(signals: Sequence[SignalResult]) -> list[str]:
    out: list[str] = []
    for signal in signals:
        text = SUGGESTIONS.get(signal.id)
        if text is None:
            continue
        wanted = signal.direction == "negative" or (
            signal.id == "linked_issue" and signal.direction == "neutral"
        )
        if wanted:
            out.append(text)
    return out


def _lines(item: Evidence) -> str:
    if item.line_start == item.line_end:
        return f"{item.path}:L{item.line_start}"
    return f"{item.path}:L{item.line_start}-L{item.line_end}"


def compose_reply(
    *,
    verdict: VerdictLabel,
    abstained: bool,
    evidence: Sequence[Evidence],
    signals: Sequence[SignalResult],
    contributor_note: str,
) -> str:
    parts: list[str] = []
    if abstained:
        parts.append(f"**{SUMMARY['needs_human_look']}.** {ABSTAIN_LINE}")
    else:
        parts.append(f"**{SUMMARY[verdict]}.**")

    bullets = []
    for item in evidence:
        claim = _safe(item.claim)
        if claim is not None:
            bullets.append(f"- `{_lines(item)}`: {claim}")
        if len(bullets) == MAX_BULLETS:
            break
    if bullets:
        parts.append("What we looked at:\n" + "\n".join(bullets))

    tips = [tip for tip in suggestions(signals) if _safe(tip) is not None]
    if tips:
        parts.append("Suggestions:\n" + "\n".join(f"- {tip}" for tip in tips))

    note = _safe(contributor_note[:MAX_NOTE_CHARS]) if contributor_note else None
    if contributor_note and note is None:
        note = NEUTRAL_NOTE
    if note:
        parts.append(note)
    draft = "\n\n".join(parts)
    if contains_banned(draft):  # pragma: no cover - fixed strings are clean; tripwire only
        return f"**{SUMMARY['needs_human_look']}.** {ABSTAIN_LINE}"
    return draft

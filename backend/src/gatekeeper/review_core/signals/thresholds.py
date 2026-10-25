"""Every signal threshold in one place. Change them here, never inline in a rule."""

from typing import Final

#: tiny_change: at most this many changed lines ...
TINY_CHANGE_MAX_LINES: Final = 3
#: ... while the description is at least this long (characters).
TINY_CHANGE_LONG_DESCRIPTION: Final = 500

#: whitespace_only: share of changed lines that only differ in whitespace.
WHITESPACE_WARN_SHARE: Final = 0.8

#: typo_scale: documentation-only change of at most this many changed characters.
TYPO_MAX_CHARS: Final = 20

#: description_present: bodies shorter than this (after stripping) count as "very short".
SHORT_DESCRIPTION_CHARS: Final = 40

#: ci_status: check conclusions that count as failing / passing.
FAILING_CONCLUSIONS: Final = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
PASSING_CONCLUSIONS: Final = frozenset({"success", "neutral", "skipped"})

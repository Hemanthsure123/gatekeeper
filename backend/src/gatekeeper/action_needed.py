"""The standard "ACTION NEEDED FROM USER" block (CLAUDE.md standing rule)."""

from __future__ import annotations

from collections.abc import Sequence


def action_needed(what: str, why: str, steps: Sequence[str], env_key: str | None = None) -> str:
    numbered = "\n".join(f"  {i}. {step}" for i, step in enumerate(steps, start=1))
    return (
        "=== ACTION NEEDED FROM USER ===\n"
        f"What: {what}\n"
        f"Why: {why}\n"
        f"Steps:\n{numbered}\n"
        f".env key: {env_key + ' (you add it yourself)' if env_key else 'none'}\n"
        'Reply "done" and I will continue.\n'
        "=== END ==="
    )

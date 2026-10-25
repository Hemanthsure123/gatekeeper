"""Safe edits of NON-SECRET keys in ``.env`` (``make env-set``, gemma-setup, discover-repo).

Rules:
- Only keys on ``ALLOWED_KEYS`` may be set. Secrets (anything with TOKEN, SECRET, KEY,
  PASSWORD or PRIVATE in its name, DATABASE_URL) are refused with a message telling
  the user to edit that value themselves. The few allowlisted names that contain one
  of those words are explicit, reviewed exceptions (``NAMED_LIKE_SECRET_BUT_NOT``).
- Only the matching line changes (or one line is appended); every other line and
  comment is preserved byte for byte. Existing values are never printed or returned.
- ``.env`` is never created here; ``make init-env`` does that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from gatekeeper.errors import ConfigError, ValidationFailed

REVIEW_KEYS: Final = frozenset(
    {
        "REVIEW_ABSTAIN_THRESHOLD",
        "REVIEW_NEGATIVE_MIN_CONFIDENCE",
        "REVIEW_MAX_EVIDENCE",
        "REVIEW_MAX_OUTPUT_TOKENS",
        "REVIEW_DIFF_TOKEN_BUDGET",
    }
)
ALLOWED_KEYS: Final = frozenset(
    {
        "APP_ENV",
        "LOG_LEVEL",
        "MODEL_BACKEND",
        "GEMMA_MODEL_PRIMARY",
        "GEMMA_MODEL_SECONDARY",
        "GEMINI_KEY_STRATEGY",
        "GEMINI_KEY_COOLDOWN_BASE_S",
        "GEMINI_KEY_COOLDOWN_MAX_S",
        "GEMINI_MAX_WAIT_FOR_KEY_S",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_MODEL_SECONDARY",
        "OLLAMA_NUM_CTX",
        "OLLAMA_KEEP_ALIVE",
        "MODEL_TIMEOUT_S",
        "MODEL_MAX_RETRIES",
        "MODEL_MAX_CONCURRENCY",
        "MODEL_CACHE_TTL_S",
        "GATEKEEPER_ALLOWED_REPOS",
        "ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL",
        "GITHUB_API_BASE_URL",
        "WEBHOOK_MAX_BODY_BYTES",
        "JOB_TIMEOUT_S",
        "WORKER_POLL_INTERVAL_S",
        "JOB_LEASE_S",
        "JOB_MAX_ATTEMPTS",
        "DIFF_MAX_BYTES",
        "PR_MAX_FILES",
        *REVIEW_KEYS,
    }
)
SECRET_MARKERS: Final = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PRIVATE")
#: Allowlisted names that contain a secret marker but hold no secret.
NAMED_LIKE_SECRET_BUT_NOT: Final = frozenset(
    {
        "GEMINI_KEY_STRATEGY",
        "GEMINI_KEY_COOLDOWN_BASE_S",
        "GEMINI_KEY_COOLDOWN_MAX_S",
        "GEMINI_MAX_WAIT_FOR_KEY_S",
        "ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL",
    }
)
_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_NEEDS_QUOTES = re.compile(r"[\s#'\"]")


def looks_secret(key: str) -> bool:
    return key == "DATABASE_URL" or any(marker in key for marker in SECRET_MARKERS)


def refusal(key: str) -> str | None:
    """Why ``key`` may not be set by tooling, or None if it may."""
    if key in ALLOWED_KEYS:
        return None
    if looks_secret(key):
        return f"{key} is a secret: edit it in .env yourself (this tool never sets secrets)"
    return f"{key} is not on the env-set allowlist: edit it in .env yourself if you need to"


def parse_assignments(args: Iterable[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for arg in args:
        key, sep, value = arg.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            raise ValidationFailed(f"expected KEY=VALUE, got an argument starting {arg[:20]!r}")
        if "\n" in value or "\r" in value:
            raise ValidationFailed(f"the value for {key} must be a single line")
        pairs.append((key, value.strip()))
    return pairs


def _render(key: str, value: str) -> str:
    if _NEEDS_QUOTES.search(value):
        value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{key}={value}"


def apply_updates(text: str, updates: Sequence[tuple[str, str]]) -> str:
    """Return ``text`` with each key's line replaced (or appended). Pure function."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    for key, value in updates:
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        rendered = _render(key, value)
        found = False
        for index, line in enumerate(lines):
            if pattern.match(line):
                ending = line[len(line.rstrip("\r\n")) :] or newline
                lines[index] = rendered + ending
                found = True
        if not found:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines.append(rendered + newline)
    return "".join(lines)


def set_values(updates: Sequence[tuple[str, str]], env_path: Path = Path(".env")) -> list[str]:
    """Validate everything first, then write once. Returns the keys that were set."""
    problems = [reason for key, _ in updates if (reason := refusal(key)) is not None]
    if problems:
        raise ValidationFailed("; ".join(problems), details={"keys": [k for k, _ in updates]})
    if not env_path.exists():
        raise ConfigError("no .env file found: run `make init-env` first")
    text = env_path.read_bytes().decode("utf-8")
    env_path.write_bytes(apply_updates(text, updates).encode("utf-8"))
    return [key for key, _ in updates]

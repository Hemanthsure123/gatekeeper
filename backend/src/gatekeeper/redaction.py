"""Redaction shared by logging and the audit trail.

The :class:`Redactor` masks, in this order:
1. any exact secret value known to Settings (longest first);
2. PEM private key blocks;
3. GitHub token formats (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_);
4. bearer tokens (the scheme word is kept, the credential is masked);
5. email addresses.

Patterns are deliberately conservative so ordinary text (code, prose, version
strings, decorators, short words after "bearer") passes through unchanged.
Dict keys are structure, not data, and are never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gatekeeper.config import Settings

MIN_SECRET_LENGTH = 8


def mask(kind: str) -> str:
    return f"[REDACTED:{kind}]"


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S
)
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")
_BEARER = re.compile(r"\b([Bb]earer|BEARER)(\s+)[A-Za-z0-9\-._~+/]{16,}=*")
_EMAIL = re.compile(
    r"(?<![\w.+%-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)


class Redactor:
    """Masks secrets and personal data in strings and JSON-like structures."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True)
        self._secrets = re.compile("|".join(map(re.escape, values))) if values else None

    @classmethod
    def from_settings(cls, settings: Settings) -> Redactor:
        return cls(settings.secret_values())

    def redact_text(self, text: str) -> str:
        if self._secrets is not None:
            text = self._secrets.sub(mask("secret"), text)
        text = _PRIVATE_KEY.sub(mask("private_key"), text)
        text = _GITHUB_TOKEN.sub(mask("github_token"), text)
        text = _BEARER.sub(lambda m: f"{m[1]}{m[2]}{mask('bearer_token')}", text)
        return _EMAIL.sub(mask("email"), text)

    def redact(self, value: Any) -> Any:
        """Return a redacted copy of a JSON-like value (dicts, lists, tuples, scalars)."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact(item) for item in value]
        return value

"""Request validators for the model adapter.

``forbidden_terms_guard`` is defence in depth behind the identity scrub: it rejects any
request whose system text or text parts contain a forbidden term (case-insensitive,
whole word), with IdentityLeakBlocked (a PolicyViolation, code ``identity_leak_blocked``),
before the backend is called. Callers hand it an
opaque set of strings; the guard does not know or log what they are.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from gatekeeper.errors import IdentityLeakBlocked
from gatekeeper.models.adapter import RequestValidator
from gatekeeper.models.types import ModelRequest


def _pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    cleaned = sorted({term.strip() for term in terms if term.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    alternatives = "|".join(re.escape(term) for term in cleaned)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)


def forbidden_terms_guard(terms: Iterable[str]) -> RequestValidator:
    pattern = _pattern(terms)

    def guard(request: ModelRequest[Any]) -> None:
        if pattern is None:
            return
        for text in (request.system, *request.text_parts):
            if pattern.search(text):
                raise IdentityLeakBlocked(
                    "model request blocked: it contains a forbidden term (identity leak guard)",
                    details={"prompt_id": request.prompt_id},
                )

    return guard

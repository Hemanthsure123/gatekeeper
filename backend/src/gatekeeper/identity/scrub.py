"""Identity scrub: the trusted boundary between GitHub data and the review core.

Runs in the ``process_pr`` handler (outside the forbidden packages) BEFORE content is
stored or reviewed. It may use identity data; nothing downstream sees it.

Title and body:
- e-mail addresses -> ``[email]``;
- ``github.com/<login>`` profile URLs (one path segment) -> ``github.com/[user]``;
- ``@org/team`` -> ``@[team]`` and any ``@login`` mention -> ``@[user]``;
- the author's login as a word -> ``[author]``.

Patches: only e-mail addresses and the author's login. ``@name`` in code is left alone
(decorators, annotations, npm scopes).

Paths, labels and check names: only the author's login, so the leak guard never trips
on a legitimate field.

Limits: real names, nicknames, other handles and identity inside image URLs are not
caught. Logins shorter than ``MIN_TEXT_LOGIN`` characters are not replaced as plain
words (they would destroy ordinary text); mentions still are.
"""

from __future__ import annotations

import re
from typing import Final

from gatekeeper.github.normalize import AuthorInfo, CheckSummary, FileChange, ItemContent

AUTHOR: Final = "[author]"
USER_MENTION: Final = "@[user]"
TEAM_MENTION: Final = "@[team]"
PROFILE: Final = "github.com/[user]"
EMAIL: Final = "[email]"
MIN_TEXT_LOGIN: Final = 3

_EMAIL = re.compile(
    r"(?<![\w.+%-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)
_PROFILE_URL = re.compile(
    r"\bgithub\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?![A-Za-z0-9-]|/[A-Za-z0-9._-])",
    re.IGNORECASE,
)
_TEAM = re.compile(r"(?<![\w@./-])@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+")
_MENTION = re.compile(r"(?<![\w@./-])@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?![A-Za-z0-9-])")


def login_pattern(login: str) -> re.Pattern[str]:
    """Case-insensitive match not glued to other letters or digits. ``-`` and ``_`` act
    as separators, so ``from-<login>`` is caught; ``<login>s`` or ``x<login>`` is not."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(login)}(?![A-Za-z0-9])", re.IGNORECASE)


def forbidden_terms(author: AuthorInfo) -> frozenset[str]:
    """Opaque strings the leak guard rejects in outgoing model text."""
    login = author.login.strip()
    return frozenset({login.lower()}) if len(login) >= MIN_TEXT_LOGIN else frozenset()


class _Scrubber:
    def __init__(self, login: str) -> None:
        self._login = login_pattern(login) if len(login) >= MIN_TEXT_LOGIN else None

    def login(self, text: str) -> str:
        return self._login.sub(AUTHOR, text) if self._login else text

    def code(self, text: str) -> str:
        return self.login(_EMAIL.sub(EMAIL, text))

    def prose(self, text: str) -> str:
        text = _EMAIL.sub(EMAIL, text)
        text = _PROFILE_URL.sub(PROFILE, text)
        text = _TEAM.sub(TEAM_MENTION, text)
        text = _MENTION.sub(USER_MENTION, text)
        return self.login(text)


def scrub_content(content: ItemContent, author: AuthorInfo) -> ItemContent:
    """Return a scrubbed copy of ``content``; the input is never mutated."""
    s = _Scrubber(author.login.strip())
    files = [
        FileChange(
            **{
                **file.model_dump(),
                "path": s.login(file.path),
                "patch": s.code(file.patch) if file.patch is not None else None,
            }
        )
        for file in content.files
    ]
    checks = [
        CheckSummary(name=s.login(c.name), status=c.status, conclusion=c.conclusion)
        for c in content.checks
    ]
    return content.model_copy(
        update={
            "title": s.prose(content.title),
            "body": s.prose(content.body),
            "labels": [s.login(label) for label in content.labels],
            "files": files,
            "checks": checks,
        }
    )

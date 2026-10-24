"""The contributor-blind boundary: split a pull request into identity-free content and
author identity.

``ItemContent`` goes to ``items.content`` and may reach the review core. It carries no
user logins or names, avatars, reviewers, commenters, commit authors, nor the fork's
``head.label`` / branch names (those can contain logins). ``AuthorInfo`` goes only to
``item_authors``.

Not done here: scrubbing the author's login out of free text (title, body, patches).
The intake step does that in a later prompt.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

from gatekeeper.audit.hashing import canonical_json, sha256_hex
from gatekeeper.db.models import find_identity_keys
from gatekeeper.errors import ValidationFailed
from gatekeeper.github.models import CheckRun, PullFiles, PullRequest

#: Associations GitHub reports for someone with no previous contribution to the repo.
#: Limits: GitHub computes them per repository at event time, and FIRST_TIMER means a
#: first contribution anywhere on GitHub. MANNEQUIN/NONE are not treated as "first".
FIRST_CONTRIBUTION_ASSOCIATIONS: Final = frozenset({"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"})

_CLOSING_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE
)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?(https?://[^\s)>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FileChange(_Frozen):
    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    #: GitHub sent no patch (binary, or too large for the API).
    patch_missing: bool
    #: We dropped the patch because DIFF_MAX_BYTES was reached.
    patch_omitted: bool
    #: Heuristic: no patch and no line changes on an added/modified file.
    binary: bool


class CheckSummary(_Frozen):
    name: str
    status: str
    conclusion: str | None


class Totals(_Frozen):
    files: int
    additions: int
    deletions: int


class ItemContent(_Frozen):
    title: str
    body: str
    base_sha: str
    head_sha: str
    draft: bool
    labels: list[str]
    files: list[FileChange]
    totals: Totals
    diff_truncated: bool
    truncation_reasons: list[str]
    checks: list[CheckSummary]
    referenced_issues: list[int]
    image_urls: list[str]


class AuthorInfo(_Frozen):
    login: str
    github_user_id: int
    account_created_at: datetime | None
    author_association: str | None
    is_first_contribution: bool | None


def referenced_issues(body: str) -> list[int]:
    """Issue numbers referenced with closing keywords ("fixes #12"), same repo only."""
    return sorted({int(number) for number in _CLOSING_REF.findall(body)})


def image_urls(body: str) -> list[str]:
    """Image URLs in Markdown or HTML, in order, without duplicates. Never downloaded."""
    urls = sorted(
        [(m.start(), m[1]) for m in _MARKDOWN_IMAGE.finditer(body)]
        + [(m.start(), m[1]) for m in _HTML_IMAGE.finditer(body)]
    )
    return list(dict.fromkeys(url for _, url in urls))


def is_first_contribution(association: str | None) -> bool | None:
    return None if association is None else association.upper() in FIRST_CONTRIBUTION_ASSOCIATIONS


def content_hash(content: ItemContent) -> str:
    return sha256_hex(canonical_json(content.model_dump(mode="json")))


def normalize_pull(
    pr: PullRequest,
    files: PullFiles,
    checks: Sequence[CheckRun],
    *,
    diff_max_bytes: int,
    account_created_at: datetime | None,
) -> tuple[ItemContent, AuthorInfo]:
    reasons: list[str] = []
    if files.truncated:
        reasons.append("pr_max_files")
    elif pr.changed_files > len(files.files):
        reasons.append("github_file_limit")

    changes: list[FileChange] = []
    budget = diff_max_bytes
    for file in files.files:
        patch = file.patch
        omitted = False
        if patch is not None:
            size = len(patch.encode("utf-8"))
            if size > budget:
                patch, omitted = None, True
                if "diff_max_bytes" not in reasons:
                    reasons.append("diff_max_bytes")
            else:
                budget -= size
        missing = file.patch is None
        changes.append(
            FileChange(
                path=file.filename,
                status=file.status,
                additions=file.additions,
                deletions=file.deletions,
                patch=patch,
                patch_missing=missing,
                patch_omitted=omitted,
                binary=missing and file.changes == 0 and file.status in {"added", "modified"},
            )
        )

    body = pr.body or ""
    content = ItemContent(
        title=pr.title,
        body=body,
        base_sha=pr.base.sha,
        head_sha=pr.head.sha,
        draft=pr.draft,
        labels=[label.name for label in pr.labels],
        files=changes,
        totals=Totals(files=pr.changed_files, additions=pr.additions, deletions=pr.deletions),
        diff_truncated=bool(reasons),
        truncation_reasons=reasons,
        checks=[
            CheckSummary(name=c.name, status=c.status, conclusion=c.conclusion) for c in checks
        ],
        referenced_issues=referenced_issues(body),
        image_urls=image_urls(body),
    )
    leaked = find_identity_keys(content.model_dump(mode="json"))
    if leaked:  # pragma: no cover - guarded by the model's fields; kept as a tripwire
        raise ValidationFailed("identity fields in item content", details={"paths": leaked})
    author = AuthorInfo(
        login=pr.user.login,
        github_user_id=pr.user.id,
        account_created_at=account_created_at,
        author_association=pr.author_association,
        is_first_contribution=is_first_contribution(pr.author_association),
    )
    return content, author

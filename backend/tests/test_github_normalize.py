"""Normalisation: identity-free content, author info apart, truncation flags. Synthetic data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gatekeeper.db.models import IDENTITY_KEYS, Item, find_identity_keys
from gatekeeper.github.models import CheckRun, PullFile, PullFiles, PullRequest
from gatekeeper.github.normalize import (
    content_hash,
    image_urls,
    is_first_contribution,
    normalize_pull,
    referenced_issues,
)

LOGIN = "synthetic-contributor"


def pull(**overrides: Any) -> PullRequest:
    data: dict[str, Any] = {
        "number": 12,
        "state": "open",
        "title": "Improve parser",
        "body": "Fixes #3 and resolves: #9. See ![shot](https://img.example.test/a.png)",
        "draft": True,
        "user": {"login": LOGIN, "id": 4242, "avatar_url": "https://avatars.example.test/u/4242"},
        "author_association": "FIRST_TIME_CONTRIBUTOR",
        "labels": [{"name": "bug"}, {"name": "parser"}],
        "base": {"sha": "a" * 40, "ref": "main", "repo": {"id": 1, "full_name": "octo/play"}},
        "head": {"sha": "b" * 40, "ref": f"{LOGIN}-patch-1", "label": f"{LOGIN}:{LOGIN}-patch-1"},
        "requested_reviewers": [{"login": "synthetic-reviewer"}],
        "assignees": [{"login": "synthetic-assignee"}],
        "additions": 12,
        "deletions": 2,
        "changed_files": 3,
    }
    data.update(overrides)
    return PullRequest.model_validate(data)


FILES = PullFiles(
    files=[
        PullFile(
            filename="src/parser.py",
            status="modified",
            additions=10,
            deletions=2,
            changes=12,
            patch="@@ -1,2 +1,10 @@\n+code",
        ),
        PullFile(filename="assets/logo.png", status="added", additions=0, deletions=0, changes=0),
        PullFile(filename="data/huge.csv", status="modified", additions=2, deletions=0, changes=2),
    ],
    truncated=False,
)
CHECKS = [CheckRun(name="tests", status="completed", conclusion="success")]


def normalize(**kwargs: Any) -> Any:
    options: dict[str, Any] = {"diff_max_bytes": 300_000, "account_created_at": None}
    options.update(kwargs)
    pr = options.pop("pr", pull())
    files = options.pop("files", FILES)
    return normalize_pull(pr, files, CHECKS, **options)


def test_content_has_no_identity_anywhere() -> None:
    content, _ = normalize()
    dumped = content.model_dump(mode="json")
    assert find_identity_keys(dumped) == []
    serialized = json.dumps(dumped)
    for leak in (
        LOGIN,
        "synthetic-reviewer",
        "synthetic-assignee",
        "avatars.example.test",
        "patch-1",
    ):
        assert leak not in serialized
    assert not set(dumped) & IDENTITY_KEYS
    Item(content=dumped)  # the ORM identity guard accepts it


def test_content_fields() -> None:
    content, _ = normalize()
    assert content.title == "Improve parser"
    assert (content.base_sha, content.head_sha, content.draft) == ("a" * 40, "b" * 40, True)
    assert content.labels == ["bug", "parser"]
    assert content.totals.model_dump() == {"files": 3, "additions": 12, "deletions": 2}
    assert content.referenced_issues == [3, 9]
    assert content.image_urls == ["https://img.example.test/a.png"]
    assert [c.model_dump() for c in content.checks] == [
        {"name": "tests", "status": "completed", "conclusion": "success"}
    ]
    assert content.diff_truncated is False


def test_missing_patches_and_binary_files_are_flagged() -> None:
    content, _ = normalize()
    source, logo, huge = content.files
    assert (source.patch_missing, source.binary) == (False, False)
    assert (logo.patch, logo.patch_missing, logo.binary) == (None, True, True)
    assert (huge.patch_missing, huge.binary) == (True, False)  # too large for GitHub's API


def test_diff_budget_truncates_and_flags() -> None:
    content, _ = normalize(diff_max_bytes=10)
    source = content.files[0]
    assert (source.patch, source.patch_omitted) == (None, True)
    assert content.diff_truncated is True
    assert content.truncation_reasons == ["diff_max_bytes"]


def test_file_limits_flag_truncation() -> None:
    capped = PullFiles(files=FILES.files[:1], truncated=True)
    content, _ = normalize(files=capped)
    assert content.truncation_reasons == ["pr_max_files"]
    short = PullFiles(files=FILES.files[:2], truncated=False)
    content, _ = normalize(files=short)
    assert content.truncation_reasons == ["github_file_limit"]


def test_author_info_is_separate() -> None:
    created = datetime(2021, 5, 6, tzinfo=UTC)
    _, author = normalize(account_created_at=created)
    assert author.model_dump() == {
        "login": LOGIN,
        "github_user_id": 4242,
        "account_created_at": created,
        "author_association": "FIRST_TIME_CONTRIBUTOR",
        "is_first_contribution": True,
    }


@pytest.mark.parametrize(
    ("association", "expected"),
    [
        ("FIRST_TIME_CONTRIBUTOR", True),
        ("FIRST_TIMER", True),
        ("CONTRIBUTOR", False),
        ("NONE", False),
        (None, None),
    ],
)
def test_first_contribution_mapping(association: str | None, expected: bool | None) -> None:
    assert is_first_contribution(association) is expected


def test_content_hash_is_stable_and_sensitive() -> None:
    first, _ = normalize()
    again, _ = normalize()
    other, _ = normalize(pr=pull(title="Different"))
    assert content_hash(first) == content_hash(again)
    assert content_hash(first) != content_hash(other)
    assert len(content_hash(first)) == 64


def test_reference_and_image_parsing() -> None:
    body = (
        "closes #1, Fixed #22; resolve #3 but not 'see #4' or 'fixes octo/other#5'.\n"
        '<img width="10" src="https://img.example.test/b.png"> '
        '![x](https://img.example.test/a.png "t")'
        " ![dup](https://img.example.test/a.png) ![rel](./local.png)"
    )
    assert referenced_issues(body) == [1, 3, 22]
    assert image_urls(body) == ["https://img.example.test/b.png", "https://img.example.test/a.png"]
    assert normalize(pr=pull(body=None))[0].body == ""

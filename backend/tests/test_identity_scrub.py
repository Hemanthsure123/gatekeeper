"""Identity scrub rules. Synthetic logins and addresses only."""

from __future__ import annotations

import pytest

from gatekeeper.github.normalize import (
    AuthorInfo,
    CheckSummary,
    FileChange,
    ItemContent,
    Totals,
)
from gatekeeper.identity.scrub import forbidden_terms, login_pattern, scrub_content

LOGIN = "Synth-Dev"


def author(login: str = LOGIN) -> AuthorInfo:
    return AuthorInfo(
        login=login,
        github_user_id=1,
        account_created_at=None,
        author_association=None,
        is_first_contribution=None,
    )


def content(
    title: str = "", body: str = "", patch: str | None = None, path: str = "a.py"
) -> ItemContent:
    return ItemContent(
        title=title,
        body=body,
        base_sha="a" * 40,
        head_sha="b" * 40,
        draft=False,
        labels=[f"from-{LOGIN}"],
        files=[
            FileChange(
                path=path,
                status="modified",
                additions=1,
                deletions=0,
                patch=patch,
                patch_missing=patch is None,
                patch_omitted=False,
                binary=False,
            )
        ],
        totals=Totals(files=1, additions=1, deletions=0),
        diff_truncated=False,
        truncation_reasons=[],
        checks=[CheckSummary(name=f"ci-{LOGIN}", status="completed", conclusion="success")],
        referenced_issues=[],
        image_urls=[],
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Fix by {LOGIN}.", "Fix by [author]."),
        (f"fix by {LOGIN.lower()} and {LOGIN.upper()}", "fix by [author] and [author]"),
        (f"thanks @{LOGIN} and @other-person", "thanks @[user] and @[user]"),
        ("cc @octo-org/reviewers please", "cc @[team] please"),
        (f"see https://github.com/{LOGIN} for more", "see https://github.com/[user] for more"),
        ("repo https://github.com/octo/play stays", "repo https://github.com/octo/play stays"),
        ("mail synth.dev+x@example.test now", "mail [email] now"),
        (
            f"{LOGIN}x and x{LOGIN} are different words",
            f"{LOGIN}x and x{LOGIN} are different words",
        ),
    ],
)
def test_title_and_body_rules(text: str, expected: str) -> None:
    scrubbed = scrub_content(content(title=text, body=text), author())
    assert scrubbed.title == expected
    assert scrubbed.body == expected


def test_patches_keep_decorators_but_lose_login_and_emails() -> None:
    patch = (
        "@@ -1,2 +1,3 @@\n"
        "+@pytest.fixture\n"
        "+# Author: Synth-Dev <synth@example.test>\n"
        " @dataclass\n"
        "+import '@types/node'\n"
    )
    scrubbed = scrub_content(content(patch=patch), author()).files[0].patch
    assert scrubbed is not None
    assert "@pytest.fixture" in scrubbed
    assert " @dataclass" in scrubbed
    assert "@types/node" in scrubbed
    assert "# Author: [author] <[email]>" in scrubbed
    assert LOGIN.lower() not in scrubbed.lower()


def test_paths_labels_and_checks_lose_the_login() -> None:
    scrubbed = scrub_content(content(path=f"notes/{LOGIN}/todo.md"), author())
    assert scrubbed.files[0].path == "notes/[author]/todo.md"
    assert scrubbed.labels == ["from-[author]"]
    assert scrubbed.checks[0].name == "ci-[author]"


def test_input_is_never_mutated() -> None:
    original = content(title=f"by {LOGIN}", patch=f"+{LOGIN}")
    snapshot = original.model_dump()
    scrub_content(original, author())
    assert original.model_dump() == snapshot


def test_short_logins_are_not_replaced_as_words() -> None:
    scrubbed = scrub_content(content(body="a cat sat on a mat, ping @ab"), author("ab"))
    assert scrubbed.body == "a cat sat on a mat, ping @[user]"
    assert forbidden_terms(author("ab")) == frozenset()


def test_forbidden_terms_and_pattern() -> None:
    assert forbidden_terms(author()) == frozenset({LOGIN.lower()})
    pattern = login_pattern(LOGIN)
    assert pattern.search(f"hi {LOGIN.lower()}!")
    assert pattern.search(f"hi {LOGIN}-bot")
    assert not pattern.search(f"hi {LOGIN}s")

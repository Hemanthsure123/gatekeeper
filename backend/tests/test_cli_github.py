"""whoami, discover-repo and list-prs against mocked GitHub (respx). Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

from gatekeeper import cli_github
from gatekeeper.config import Settings
from gatekeeper.errors import ValidationFailed
from gatekeeper.github.client import GitHubClient

API = "https://api.github.test"
TOKEN = "synthetic-token-never-printed-77"


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {"github_api_base_url": API, "github_token_dev": TOKEN}
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def client(s: Settings) -> GitHubClient:
    return GitHubClient(s, sleep=lambda _s: None)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/user").respond(json={"login": "synthetic-user", "id": 5})
        router.get(f"{API}/rate_limit").respond(
            json={"resources": {"core": {"limit": 5000, "remaining": 4990, "reset": 1_800_000_000}}}
        )
        yield router


def repos(*names: str) -> list[dict[str, Any]]:
    return [{"full_name": n, "private": False} for n in names]


def test_whoami_prints_account_limits_repos_and_access(
    mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(f"{API}/user/repos").respond(json=repos("synthetic-user/gatekeeper-playground"))
    mock.get(f"{API}/repos/synthetic-user/gatekeeper-playground/pulls").respond(json=[])
    mock.get(f"{API}/repos/synthetic-user/gatekeeper-playground/issues").respond(403, json={})
    s = settings()
    assert cli_github.cmd_whoami(s, client(s)) == 0
    out = capsys.readouterr().out
    assert "Account:    synthetic-user" in out
    assert "Auth mode:  token" in out
    assert "4990/5000 remaining" in out
    assert "synthetic-user/gatekeeper-playground" in out
    assert "Pull requests on synthetic-user/gatekeeper-playground: readable" in out
    assert "Issues on synthetic-user/gatekeeper-playground: NOT readable" in out
    assert TOKEN not in out


def test_whoami_without_token_asks_for_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_github.cmd_whoami(settings(github_token_dev=None)) == 2
    out = capsys.readouterr().out
    assert "=== ACTION NEEDED FROM USER ===" in out
    assert ".env key: GITHUB_TOKEN_DEV" in out
    assert "Pull requests: Read-only" in out


def test_discover_sets_the_only_repo(
    mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(f"{API}/user/repos").respond(json=repos("octo/gatekeeper-playground"))
    env = tmp_path / ".env"
    env.write_text("GATEKEEPER_ALLOWED_REPOS=\n", encoding="utf-8")
    s = settings()
    assert cli_github.cmd_discover_repo(s, client(s), env) == 0
    assert (
        env.read_text(encoding="utf-8") == "GATEKEEPER_ALLOWED_REPOS=octo/gatekeeper-playground\n"
    )
    assert "set GATEKEEPER_ALLOWED_REPOS" in capsys.readouterr().out


def test_discover_lists_choices_when_several(
    mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(f"{API}/user/repos").respond(json=repos("octo/a", "octo/b"))
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    s = settings()
    assert cli_github.cmd_discover_repo(s, client(s), env) == 0
    out = capsys.readouterr().out
    assert 'make env-set ARGS="GATEKEEPER_ALLOWED_REPOS=octo/b"' in out
    assert env.read_text(encoding="utf-8") == ""


def test_discover_with_no_repos_asks_for_access(
    mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(f"{API}/user/repos").respond(json=[])
    s = settings()
    assert cli_github.cmd_discover_repo(s, client(s), tmp_path / ".env") == 1
    assert "=== ACTION NEEDED FROM USER ===" in capsys.readouterr().out


def test_discover_keeps_an_existing_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_github.cmd_discover_repo(settings(gatekeeper_allowed_repos="octo/a")) == 0
    assert "already set: octo/a" in capsys.readouterr().out


def test_list_prs(mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]) -> None:
    mock.get(f"{API}/repos/octo/play/pulls").respond(
        json=[
            {"number": 3, "title": "Fix bug", "draft": False},
            {"number": 4, "title": "WIP", "draft": True},
        ]
    )
    s = settings()
    assert cli_github.cmd_list_prs(s, "octo/play", client(s)) == 0
    out = capsys.readouterr().out
    assert "#3     Fix bug" in out
    assert "#4     [draft] WIP" in out
    with pytest.raises(ValidationFailed):
        cli_github.cmd_list_prs(s, "not a repo", client(s))

"""CLI argument parsing and dispatch (no database or network)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from gatekeeper import cli
from gatekeeper.config import Settings
from gatekeeper.errors import ExternalServiceError


def use_settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    base: dict[str, Any] = {"gatekeeper_allowed_repos": "octo/play"}
    base.update(values)
    settings = Settings(_env_file=None, **base)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _settings: None)
    return settings


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["fetch-pr", "octo/play#3"], {"command": "fetch-pr", "pr": "octo/play#3"}),
        (["enqueue-pr", "https://github.com/octo/play/pull/3"], {"command": "enqueue-pr"}),
        (["audit", "00000000-0000-0000-0000-000000000001"], {"run_id": UUID(int=1)}),
        (["jobs", "--limit", "5"], {"command": "jobs", "limit": 5}),
    ],
)
def test_parser(argv: list[str], expected: dict[str, Any]) -> None:
    args = vars(cli.build_parser().parse_args(argv))
    assert {key: args[key] for key in expected} == expected


@pytest.mark.parametrize("argv", [[], ["audit", "not-a-uuid"], ["fetch-pr"], ["unknown"]])
def test_parser_rejects_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


def test_fetch_pr_refuses_repos_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    assert cli.main(["fetch-pr", "someone/else#1"]) == 1
    assert "GATEKEEPER_ALLOWED_REPOS" in capsys.readouterr().err


def test_invalid_reference_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    assert cli.main(["enqueue-pr", "not a pr"]) == 2
    assert "validation_failed" in capsys.readouterr().err


def test_fetch_pr_prints_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    calls: list[str] = []

    class Result:
        analysis_id = UUID(int=7)
        run_id = UUID(int=8)
        status = "completed"

    def process(_services: object, ref: object, trigger: str) -> Result:
        calls.append(f"{ref}:{trigger}")
        return Result()

    monkeypatch.setattr("gatekeeper.cli.Services.from_settings", lambda _settings: object())
    monkeypatch.setattr(cli, "process_pr", process)
    assert cli.main(["fetch-pr", "https://github.com/octo/play/pull/9"]) == 0
    out = capsys.readouterr().out
    assert calls == ["octo/play#9:cli"]
    assert str(UUID(int=7)) in out
    assert "completed" in out


def test_external_failures_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ExternalServiceError("GitHub down")

    monkeypatch.setattr("gatekeeper.cli.Services.from_settings", lambda _settings: object())
    monkeypatch.setattr(cli, "process_pr", fail)
    assert cli.main(["fetch-pr", "octo/play#9"]) == 1
    assert "external_service_error" in capsys.readouterr().err

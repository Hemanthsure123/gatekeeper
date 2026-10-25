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
        (["show", "00000000-0000-0000-0000-000000000002", "--json"], {"json": True}),
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


SUMMARY: dict[str, Any] = {
    "analysis_id": str(UUID(int=7)),
    "run_id": str(UUID(int=8)),
    "status": "completed",
    "verdict": "looks_valuable",
    "confidence": 0.81,
    "head_sha": "a" * 40,
    "model": "gemma4:e4b",
    "error_code": None,
    "result": {
        "abstain_reason": None,
        "evidence": [
            {
                "path": "src/a.py",
                "line_start": 3,
                "line_end": 4,
                "polarity": "supports_value",
                "claim": "Adds validation.",
            }
        ],
        "dropped_evidence_count": 1,
        "signals": [
            {
                "id": "ci_status",
                "direction": "positive",
                "explanation": "All CI checks passed.",
                "value": 1,
            },
            {"id": "docs_only", "direction": "neutral", "explanation": "x", "value": False},
        ],
        "reply_draft": "**Looks valuable.**",
        "latency_ms": 41_200.0,
        "cache_hit": False,
    },
}


def test_format_analysis() -> None:
    text = cli.format_analysis(SUMMARY)
    assert "Verdict:    looks_valuable (completed)" in text
    assert "Confidence: 0.81" in text
    assert "Evidence (1 verified, 1 dropped):" in text
    assert "src/a.py:L3-L4 [supports_value] Adds validation." in text
    assert "ci_status (positive)" in text
    assert "docs_only" not in text
    assert "gemma4:e4b, 41.2 s" in text
    assert f"gatekeeper audit {UUID(int=8)}" in text
    assert "Reply draft (not posted):" in text


def test_fetch_pr_prints_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    calls: list[str] = []

    class Result:
        analysis_id = UUID(int=7)
        run_id = UUID(int=8)
        status = "completed"
        skipped = False

    def process(_services: object, ref: object, trigger: str) -> Result:
        calls.append(f"{ref}:{trigger}")
        return Result()

    monkeypatch.setattr("gatekeeper.cli.Services.from_settings", lambda _settings: object())
    monkeypatch.setattr(cli, "process_pr", process)
    monkeypatch.setattr(cli, "load_analysis", lambda _settings, _id: SUMMARY)
    assert cli.main(["fetch-pr", "https://github.com/octo/play/pull/9"]) == 0
    assert calls == ["octo/play#9:cli"]
    assert "Verdict:    looks_valuable" in capsys.readouterr().out
    assert cli.main(["fetch-pr", "octo/play#9", "--json"]) == 0
    assert '"verdict": "looks_valuable"' in capsys.readouterr().out


def test_show(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    use_settings(monkeypatch)
    monkeypatch.setattr(cli, "load_analysis", lambda _settings, _id: SUMMARY)
    assert cli.main(["show", str(UUID(int=7))]) == 0
    assert "Confidence: 0.81" in capsys.readouterr().out
    monkeypatch.setattr(cli, "load_analysis", lambda _settings, _id: None)
    assert cli.main(["show", str(UUID(int=7)), "--json"]) == 1


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

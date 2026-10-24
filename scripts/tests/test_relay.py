"""Tests for scripts/relay.py (no Node or network needed)."""

from __future__ import annotations

from typing import Any

import pytest

import relay
from gatekeeper.config import Settings

CHANNEL = "https://smee.io/synthetic-channel-id"


def use_settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    monkeypatch.setattr(relay, "get_settings", lambda: settings)


def test_missing_url_explains_what_to_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    assert relay.main([]) == 2
    assert "WEBHOOK_PROXY_URL=" in capsys.readouterr().out


def test_missing_node_explains_what_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, webhook_proxy_url=CHANNEL)
    monkeypatch.setattr(relay.shutil, "which", lambda _name: None)
    assert relay.main([]) == 2
    assert "Node.js" in capsys.readouterr().out


def test_runs_pinned_smee_client_without_printing_the_channel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, webhook_proxy_url=CHANNEL)
    monkeypatch.setattr(relay.shutil, "which", lambda _name: "npx")
    commands: list[list[str]] = []
    monkeypatch.setattr(relay.subprocess, "call", lambda cmd: commands.append(cmd) or 0)
    assert relay.main(["--port", "8123"]) == 0
    assert commands == [
        [
            "npx",
            "--yes",
            "smee-client@5.0.0",
            "--url",
            CHANNEL,
            "--target",
            "http://127.0.0.1:8123/webhooks/github",
        ]
    ]
    assert CHANNEL not in capsys.readouterr().out

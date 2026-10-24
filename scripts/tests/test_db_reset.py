"""Tests for scripts/db_reset.py: it must refuse outside development."""

from __future__ import annotations

import pytest

import db_reset
from gatekeeper.config import Settings


@pytest.mark.parametrize("app_env", ["production", "test"])
def test_refuses_outside_development(app_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(_env_file=None, app_env=app_env)  # type: ignore[call-arg]
    assert db_reset.main(settings) == 1
    assert "Refusing to reset" in capsys.readouterr().err


def test_allows_development_only() -> None:
    assert db_reset.reset_allowed(Settings(_env_file=None, app_env="development"))  # type: ignore[call-arg]

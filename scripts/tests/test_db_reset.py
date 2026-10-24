"""Tests for scripts/db_reset.py: it must refuse outside development."""

from __future__ import annotations

from typing import Any

import pytest

import db_reset
from gatekeeper.config import Settings


def make_settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize("app_env", ["production", "test"])
def test_refuses_outside_development(app_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert db_reset.main(make_settings(app_env=app_env)) == 1
    assert "Refusing to reset" in capsys.readouterr().err


def test_allows_development_only() -> None:
    assert db_reset.reset_allowed(make_settings(app_env="development"))

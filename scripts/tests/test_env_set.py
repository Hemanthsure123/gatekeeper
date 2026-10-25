"""env-set: allowlist, refusals, exact line edits, no value printing, no file creation."""

from __future__ import annotations

from pathlib import Path

import pytest

import env_set
from gatekeeper.envfile import (
    ALLOWED_KEYS,
    NAMED_LIKE_SECRET_BUT_NOT,
    apply_updates,
    looks_secret,
    refusal,
)

SECRET = "existing-value-must-not-print-1234"
ORIGINAL = (
    "# comment line\n"
    f"GITHUB_TOKEN_DEV={SECRET}\n"
    "MODEL_BACKEND=ollama\n"
    "\n"
    "# Optional: something\n"
    "GEMMA_MODEL_PRIMARY=\n"
)


@pytest.fixture
def env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ORIGINAL, encoding="utf-8", newline="\n")
    return path


def run(env_path: Path, *args: str) -> int:
    return env_set.main(list(args), env_path)


def test_sets_allowed_keys_and_preserves_everything_else(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "MODEL_BACKEND=gemini_api", "GEMMA_MODEL_PRIMARY=gemma-4-26b-a4b-it") == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["set MODEL_BACKEND", "set GEMMA_MODEL_PRIMARY"]
    assert SECRET not in out
    assert env.read_text(encoding="utf-8") == ORIGINAL.replace(
        "MODEL_BACKEND=ollama", "MODEL_BACKEND=gemini_api"
    ).replace("GEMMA_MODEL_PRIMARY=\n", "GEMMA_MODEL_PRIMARY=gemma-4-26b-a4b-it\n")


def test_appends_missing_keys(env: Path) -> None:
    assert run(env, "GATEKEEPER_ALLOWED_REPOS=octo/play") == 0
    text = env.read_text(encoding="utf-8")
    assert text.startswith(ORIGINAL)
    assert text.endswith("GATEKEEPER_ALLOWED_REPOS=octo/play\n")


@pytest.mark.parametrize(
    "key",
    [
        "GEMINI_API_KEYS",
        "GEMINI_API_KEY",
        "GITHUB_TOKEN_DEV",
        "DATABASE_URL",
        "SESSION_SECRET",
        "POSTGRES_PASSWORD",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    ],
)
def test_refuses_secrets_without_changing_anything(
    env: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "MODEL_BACKEND=gemini_api", f"{key}=x") == 2
    err = capsys.readouterr().err
    assert "edit it in .env yourself" in err
    assert env.read_text(encoding="utf-8") == ORIGINAL


def test_refuses_unknown_keys(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "SOMETHING_ELSE=1") == 2
    assert "not on the env-set allowlist" in capsys.readouterr().err


def test_key_named_settings_are_explicit_exceptions() -> None:
    for key in ("GEMINI_KEY_STRATEGY", "GEMINI_KEY_COOLDOWN_BASE_S", "GEMINI_MAX_WAIT_FOR_KEY_S"):
        assert looks_secret(key)
        assert refusal(key) is None
    assert {key for key in ALLOWED_KEYS if looks_secret(key)} == NAMED_LIKE_SECRET_BUT_NOT


def test_never_creates_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / ".env"
    assert run(missing, "MODEL_BACKEND=gemini_api") == 1
    assert not missing.exists()
    assert "make init-env" in capsys.readouterr().err


@pytest.mark.parametrize("arg", ["MODEL_BACKEND", "lower=1", "=x"])
def test_bad_arguments(env: Path, arg: str) -> None:
    assert run(env, arg) == 2
    assert env.read_text(encoding="utf-8") == ORIGINAL


def test_quotes_values_with_spaces_and_keeps_crlf() -> None:
    text = "A=1\r\nLOG_LEVEL=INFO\r\n"
    assert apply_updates(text, [("LOG_LEVEL", "DEBUG")]) == "A=1\r\nLOG_LEVEL=DEBUG\r\n"
    assert apply_updates("A=1", [("APP_ENV", "a b")]) == 'A=1\nAPP_ENV="a b"\n'


def test_usage_without_arguments(env: Path) -> None:
    assert run(env) == 2

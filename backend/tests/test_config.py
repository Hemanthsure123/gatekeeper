"""Tests for gatekeeper.config. They never read the real .env."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_KEYS = re.findall(
    r"^([A-Z][A-Z0-9_]*)=", (REPO_ROOT / ".env.example").read_text(encoding="utf-8"), re.M
)
SENTINEL = "sentinel-value-must-not-leak-7f3a"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key in EXAMPLE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here, so get_settings() cannot see the real one
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_every_template_key_has_a_field() -> None:
    assert {key.lower() for key in EXAMPLE_KEYS} == set(Settings.model_fields)


def test_defaults() -> None:
    settings = load()
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.model_backend == "ollama"
    assert settings.database_url is None
    assert settings.github_app_id is None


def test_reads_environment_and_ignores_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "Test")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("MODEL_BACKEND", "vllm")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("SOMETHING_UNRELATED", "x")
    settings = Settings(_env_file=None)
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.model_backend == "vllm"
    assert settings.github_app_id == 12345
    assert not hasattr(settings, "something_unrelated")


def test_reads_a_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("APP_ENV=production\nOLLAMA_MODEL=\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.app_env == "production"
    assert settings.ollama_model is None


def test_empty_strings_become_none_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("APP_ENV", "")
    settings = Settings(_env_file=None)
    assert settings.ollama_model is None
    assert settings.app_env == "development"
    assert load(redis_url="  ").redis_url is None


def test_secrets_are_secretstr_and_hidden_in_repr() -> None:
    settings = load(github_token_dev=SENTINEL, session_secret=SENTINEL + "2")
    assert isinstance(settings.github_token_dev, SecretStr)
    assert SENTINEL not in repr(settings)
    assert SENTINEL not in str(settings)
    assert sorted(settings.secret_values()) == [SENTINEL, SENTINEL + "2"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("app_env", "staging", "app_env"),
        ("log_level", "LOUD", "log_level"),
        ("model_backend", "openai", "model_backend"),
        ("ollama_base_url", "ftp://localhost:11434", "schemes"),
        ("ollama_base_url", "http://localhost:99999", "port"),
        ("redis_url", "redis://localhost:notaport/0", "port"),
        ("redis_url", "http://localhost:6379", "schemes"),
        ("webhook_proxy_url", "https://:8080", "host"),
        ("database_url", "postgresql://u:pw@localhost:15432", "database name"),
        ("database_url", "mysql://u:pw@localhost/db", "schemes"),
        ("github_app_id", "-5", "github_app_id"),
    ],
)
def test_rejects_invalid_formats(field: str, value: str, message: str) -> None:
    with pytest.raises(ValidationError) as caught:
        load(**{field: value})
    text = str(caught.value)
    assert message in text
    assert value not in text


def test_accepts_valid_formats() -> None:
    settings = load(
        database_url="postgresql+psycopg://user:pw@127.0.0.1:15432/gatekeeper",
        redis_url="redis://127.0.0.1:16379/0",
        ollama_base_url="http://localhost:11434",
        otel_exporter_otlp_endpoint="https://collector.example:4318",
    )
    assert settings.redis_url == "redis://127.0.0.1:16379/0"
    assert settings.database_url is not None


def test_require_names_missing_keys_only() -> None:
    settings = load(github_token_dev=SENTINEL, app_env="test")
    settings.require("GITHUB_TOKEN_DEV", "app_env")  # present: no error
    with pytest.raises(ConfigError) as caught:
        settings.require("GITHUB_TOKEN_DEV", "DATABASE_URL", "redis_url")
    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "REDIS_URL" in message
    assert "GITHUB_TOKEN_DEV" not in message
    assert SENTINEL not in message
    assert caught.value.code == "config_error"


def test_require_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="unknown setting"):
        load().require("NOT_A_KEY")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    assert get_settings() is get_settings()


def test_get_settings_wraps_errors_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"mysql://u:{SENTINEL}@h/db")
    monkeypatch.setenv("APP_ENV", SENTINEL)
    with pytest.raises(ConfigError) as caught:
        get_settings()
    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "APP_ENV" in message
    assert SENTINEL not in message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__

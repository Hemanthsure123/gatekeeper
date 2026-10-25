"""Tests for gatekeeper.config. They never read the real .env."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


def load(**env: Any) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


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
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.model_backend == "vllm"
    assert settings.github_app_id == 12345
    assert not hasattr(settings, "something_unrelated")


def test_reads_a_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("APP_ENV=production\nOLLAMA_MODEL=\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.app_env == "production"
    assert settings.ollama_model is None


def test_empty_strings_become_none_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("APP_ENV", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
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


def test_model_tuning_defaults_and_empty_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_TIMEOUT_S", "")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_timeout_s == 120
    assert settings.model_max_retries == 2
    assert settings.model_max_concurrency == 1
    assert settings.model_cache_ttl_s == 604_800
    assert settings.ollama_num_ctx == 16_384
    assert settings.ollama_keep_alive == "10m"


@pytest.mark.parametrize("value", ["10m", "1h30m", "300", "-1", "-1m", "0", "2.5h"])
def test_accepts_keep_alive_durations(value: str) -> None:
    assert load(ollama_keep_alive=value).ollama_keep_alive == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ollama_keep_alive", "forever"),
        ("ollama_num_ctx", "512"),
        ("model_max_concurrency", "0"),
        ("model_timeout_s", "-1"),
        ("model_max_retries", "99"),
    ],
)
def test_rejects_bad_model_tuning(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        load(**{field: value})


def test_repo_allowlist() -> None:
    settings = load(gatekeeper_allowed_repos=" Octo/Play-Ground , other/repo ")
    assert settings.allowed_repos == frozenset({"octo/play-ground", "other/repo"})
    assert settings.repo_allowed("octo", "PLAY-GROUND")
    assert not settings.repo_allowed("octo", "elsewhere")


@pytest.mark.parametrize(("app_env", "allowed"), [("development", True), ("production", False)])
def test_empty_allowlist_depends_on_environment(app_env: str, allowed: bool) -> None:
    assert load(app_env=app_env).repo_allowed("any", "repo") is allowed


@pytest.mark.parametrize("value", ["octo", "octo/repo/extra", "-bad/repo", "octo/repo,nope"])
def test_rejects_malformed_allowlist(value: str) -> None:
    with pytest.raises(ValidationError):
        load(gatekeeper_allowed_repos=value)


def test_p03_defaults() -> None:
    settings = load()
    assert settings.github_api_base_url == "https://api.github.com"
    assert settings.webhook_max_body_bytes == 10_485_760
    assert (settings.job_timeout_s, settings.worker_poll_interval_s) == (1800, 1)
    assert (settings.job_lease_s, settings.job_max_attempts) == (120, 3)
    assert (settings.diff_max_bytes, settings.pr_max_files) == (300_000, 300)


def test_review_defaults_and_derived_diff_budget() -> None:
    settings = load()
    assert (settings.review_abstain_threshold, settings.review_negative_min_confidence) == (
        0.6,
        0.75,
    )
    assert (settings.review_max_evidence, settings.review_max_output_tokens) == (5, 800)
    assert settings.review_diff_budget() == 16_384 - 800 - 2_048
    assert load(ollama_num_ctx="8192").review_diff_budget() == 8_192 - 800 - 2_048
    assert load(review_diff_token_budget="5000").review_diff_budget() == 5_000

"""Typed application settings, loaded from the process environment and a local ``.env``.

Rules:
- Every key in ``.env.example`` has a field here. Unknown variables are ignored.
- An empty value means "not set": optional keys become ``None``, keys with a default
  keep their default.
- Secrets are ``SecretStr``. Neither reprs nor error messages ever contain a value.
- Nothing is mandatory at load time. Each component declares what it needs with
  :meth:`Settings.require`, so keys introduced by later prompts are not demanded early.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gatekeeper.errors import ConfigError

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ModelBackend = Literal["ollama", "gemini_api", "vllm"]

_HTTP = frozenset({"http", "https"})
_POSTGRES = frozenset({"postgresql", "postgresql+psycopg", "postgres"})
_REDIS = frozenset({"redis", "rediss"})


def validate_url(value: str, schemes: frozenset[str], *, require_path: bool = False) -> str:
    """Check URL shape without ever echoing the URL (it may contain credentials)."""
    parts = urlsplit(value)
    if parts.scheme not in schemes:
        raise ValueError(f"must use one of the schemes: {', '.join(sorted(schemes))}")
    if not parts.hostname:
        raise ValueError("must include a host")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("port must be an integer between 1 and 65535") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    if require_path and parts.path.strip("/") == "":
        raise ValueError("must include a database name")
    return value


class Settings(BaseSettings):
    """All Gatekeeper configuration. Field names map to upper-case environment keys."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
        frozen=True,
    )

    # P01: runtime and local services
    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None
    database_url: SecretStr | None = None
    redis_url: str | None = None

    # P02 / P11: model backends
    model_backend: ModelBackend = "ollama"
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    ollama_model_secondary: str | None = None
    ollama_embed_model: str | None = None
    gemini_api_key: SecretStr | None = None
    gemma_model_primary: str | None = None
    gemma_model_secondary: str | None = None

    # P03: GitHub
    github_token_dev: SecretStr | None = None
    webhook_proxy_url: str | None = None
    github_app_id: int | None = Field(default=None, gt=0)
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None

    # P07 / P14 / P16
    hf_token: SecretStr | None = None
    gatekeeper_admin_token: SecretStr | None = None
    session_secret: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_values(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and not v.strip())}
        return data

    @field_validator("app_env", "model_backend", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("ollama_base_url", "webhook_proxy_url", "otel_exporter_otlp_endpoint")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return None if value is None else validate_url(value, _HTTP)

    @field_validator("redis_url")
    @classmethod
    def _redis_url(cls, value: str | None) -> str | None:
        return None if value is None else validate_url(value, _REDIS)

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            validate_url(value.get_secret_value(), _POSTGRES, require_path=True)
        return value

    def require(self, *keys: str) -> None:
        """Raise ConfigError naming every key in ``keys`` that is not set. Never shows values."""
        fields = type(self).model_fields
        unknown = sorted({key.upper() for key in keys if key.lower() not in fields})
        if unknown:
            raise ConfigError(f"unknown setting(s) requested: {', '.join(unknown)}")
        missing = [key.upper() for key in keys if getattr(self, key.lower()) is None]
        if missing:
            raise ConfigError(
                f"missing required setting(s): {', '.join(missing)}. "
                "Set them in .env (see .env.example; `make env-check` lists what is set).",
                details={"missing": missing},
            )

    def secret_values(self) -> list[str]:
        """Every secret value that is set, for redaction. Never log the result."""
        return [
            value.get_secret_value()
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), SecretStr) and value.get_secret_value()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process. Invalid values raise ConfigError naming the keys."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = [
            (str(error["loc"][0]).upper() if error["loc"] else "SETTINGS", error["msg"])
            for error in exc.errors(include_input=False, include_url=False)
        ]
        summary = "; ".join(f"{key}: {msg}" for key, msg in problems)
        raise ConfigError(
            f"invalid setting(s): {summary}", details={"keys": [key for key, _ in problems]}
        ) from None

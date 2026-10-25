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

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from gatekeeper.errors import ConfigError

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ModelBackend = Literal["ollama", "gemini_api", "vllm"]

#: Upper bound (in the adapter's conservative token estimate) for everything in the
#: review prompt except title, body and diff. A test keeps the real template under it.
REVIEW_PROMPT_OVERHEAD_TOKENS: Final = 2_048
MAX_GEMINI_KEYS: Final = 20

_HTTP = frozenset({"http", "https"})
_POSTGRES = frozenset({"postgresql", "postgresql+psycopg", "postgres"})
_REDIS = frozenset({"redis", "rediss"})
_REPO_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
# Ollama keep_alive: Go duration ("10m", "1h30m", "-1m") or plain seconds ("300", "-1").
_KEEP_ALIVE = re.compile(r"-?\d+|-?(\d+(\.\d+)?(ns|us|ms|s|m|h))+")


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
    #: Legacy single key; used only when GEMINI_API_KEYS is empty.
    gemini_api_key: SecretStr | None = None
    #: Comma-separated pool of keys (see gatekeeper.models.key_pool). Never logged.
    gemini_api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    gemini_key_strategy: Literal["round_robin", "primary_first"] = "round_robin"
    gemini_key_cooldown_base_s: float = Field(default=30.0, gt=0)
    gemini_key_cooldown_max_s: float = Field(default=3600.0, gt=0)
    gemini_max_wait_for_key_s: float = Field(default=60.0, ge=0)
    #: Allow sending private repositories' (scrubbed) content to a hosted model.
    allow_private_repos_on_hosted_model: bool = False
    gemma_model_primary: str | None = None
    gemma_model_secondary: str | None = None

    # P02: model adapter tuning. Optional, non-secret; empty or missing uses the default.
    model_timeout_s: float = Field(default=120.0, gt=0, le=3600)
    model_max_retries: int = Field(default=2, ge=0, le=10)
    model_max_concurrency: int = Field(default=1, ge=1, le=64)
    model_cache_ttl_s: int = Field(default=604_800, ge=1)
    # Ollama's own default is 4096 tokens below 24 GiB VRAM, far too small for PR diffs.
    # 16k fits typical PRs while keeping the KV cache affordable on CPU-only machines.
    ollama_num_ctx: int = Field(default=16_384, ge=2_048, le=1_048_576)
    ollama_keep_alive: str = "10m"

    # P03: GitHub
    github_token_dev: SecretStr | None = None
    webhook_proxy_url: str | None = None
    github_app_id: int | None = Field(default=None, gt=0)
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None
    # Optional, non-secret; empty or missing uses the default.
    gatekeeper_allowed_repos: str | None = None
    github_api_base_url: str = "https://api.github.com"
    webhook_max_body_bytes: int = Field(default=10_485_760, ge=1_024, le=26_214_400)
    job_timeout_s: float = Field(default=1800.0, gt=0, le=86_400)
    worker_poll_interval_s: float = Field(default=1.0, gt=0, le=60)
    job_lease_s: int = Field(default=120, ge=10, le=3_600)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    diff_max_bytes: int = Field(default=300_000, ge=1_000)
    pr_max_files: int = Field(default=300, ge=1, le=3_000)

    # P04: review core. Optional, non-secret; empty or missing uses the default.
    review_abstain_threshold: float = Field(default=0.6, ge=0, le=1)
    review_negative_min_confidence: float = Field(default=0.75, ge=0, le=1)
    review_max_evidence: int = Field(default=5, ge=1, le=20)
    review_max_output_tokens: int = Field(default=800, ge=64, le=8_192)
    review_diff_token_budget: int | None = Field(default=None, ge=256)

    # P06: extension API, chat and the approved comment. Optional, non-secret.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    tree_max_entries: int = Field(default=5_000, ge=100, le=100_000)
    chat_max_history: int = Field(default=8, ge=0, le=50)
    chat_max_output_tokens: int = Field(default=700, ge=64, le=8_192)
    comment_max_chars: int = Field(default=3_000, ge=200, le=65_000)

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

    @field_validator("ollama_keep_alive")
    @classmethod
    def _keep_alive(cls, value: str) -> str:
        if not _KEEP_ALIVE.fullmatch(value.strip()):
            raise ValueError("must be a duration like 10m, 1h30m, 300 (seconds) or -1")
        return value.strip()

    @field_validator("gatekeeper_allowed_repos")
    @classmethod
    def _allowed_repos(cls, value: str | None) -> str | None:
        if value is None:
            return None
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
        if any(not _REPO_NAME.fullmatch(entry) for entry in entries):
            raise ValueError("must be a comma-separated list of owner/name entries")
        return ",".join(entries) or None

    @field_validator("github_api_base_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        return validate_url(value, _HTTP).rstrip("/")

    @field_validator("gemini_api_keys", mode="before")
    @classmethod
    def _key_list(cls, value: Any) -> Any:
        """Split, strip, drop empties, de-duplicate; errors name positions, never values."""
        if not isinstance(value, str):
            return value
        keys: list[str] = []
        for position, raw in enumerate(value.split(","), start=1):
            entry = raw.strip()
            if not entry:
                continue
            if any(ch.isspace() or ch in "\"'" for ch in entry):
                raise ValueError(f"entry {position} contains whitespace or quotes")
            if entry not in keys:
                keys.append(entry)
        if len(keys) > MAX_GEMINI_KEYS:
            raise ValueError(f"at most {MAX_GEMINI_KEYS} keys are supported")
        return keys

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

    @property
    def allowed_repos(self) -> frozenset[str]:
        """Lower-cased ``owner/name`` entries from GATEKEEPER_ALLOWED_REPOS."""
        raw = self.gatekeeper_allowed_repos or ""
        return frozenset(entry.lower() for entry in raw.split(",") if entry)

    def repo_allowed(self, owner: str, name: str) -> bool:
        """Case-insensitive allowlist check. An empty list allows everything in
        development and test, and nothing in production."""
        if not self.allowed_repos:
            return self.app_env != "production"
        return f"{owner}/{name}".lower() in self.allowed_repos

    def review_diff_budget(self) -> int:
        """Token budget for the rendered diff in the review prompt.

        Default: OLLAMA_NUM_CTX minus REVIEW_MAX_OUTPUT_TOKENS minus
        REVIEW_PROMPT_OVERHEAD_TOKENS (system text, JSON schema, template and signal
        summary). The review step also subtracts the title and body estimate per item.
        """
        if self.review_diff_token_budget is not None:
            return self.review_diff_token_budget
        derived = (
            self.ollama_num_ctx - self.review_max_output_tokens - REVIEW_PROMPT_OVERHEAD_TOKENS
        )
        return max(256, derived)

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

    def gemini_keys(self) -> list[SecretStr]:
        """The key pool: GEMINI_API_KEYS if set, else the legacy GEMINI_API_KEY."""
        if self.gemini_api_keys:
            return list(self.gemini_api_keys)
        return [self.gemini_api_key] if self.gemini_api_key else []

    def secret_values(self) -> list[str]:
        """Every secret value that is set (including each pool key), for redaction."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            items = value if isinstance(value, list) else [value]
            values += [v.get_secret_value() for v in items if isinstance(v, SecretStr)]
        return [v for v in values if v]


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

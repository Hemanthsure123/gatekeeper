"""Tests for scripts/init_env.py. They only touch temporary directories, never the real .env."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import init_env

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / ".env.example"

EXPECTED_KEYS = [
    "APP_ENV",
    "LOG_LEVEL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "DATABASE_URL",
    "REDIS_URL",
    "MODEL_BACKEND",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "OLLAMA_MODEL_SECONDARY",
    "OLLAMA_EMBED_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_API_KEYS",
    "GEMINI_KEY_STRATEGY",
    "GEMINI_KEY_COOLDOWN_BASE_S",
    "GEMINI_KEY_COOLDOWN_MAX_S",
    "GEMINI_MAX_WAIT_FOR_KEY_S",
    "ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL",
    "GEMMA_MODEL_PRIMARY",
    "GEMMA_MODEL_SECONDARY",
    "MODEL_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "MODEL_MAX_CONCURRENCY",
    "MODEL_CACHE_TTL_S",
    "OLLAMA_NUM_CTX",
    "OLLAMA_KEEP_ALIVE",
    "GITHUB_TOKEN_DEV",
    "WEBHOOK_PROXY_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
    "GATEKEEPER_ALLOWED_REPOS",
    "GITHUB_API_BASE_URL",
    "WEBHOOK_MAX_BODY_BYTES",
    "JOB_TIMEOUT_S",
    "WORKER_POLL_INTERVAL_S",
    "JOB_LEASE_S",
    "JOB_MAX_ATTEMPTS",
    "DIFF_MAX_BYTES",
    "PR_MAX_FILES",
    "REVIEW_ABSTAIN_THRESHOLD",
    "REVIEW_NEGATIVE_MIN_CONFIDENCE",
    "REVIEW_MAX_EVIDENCE",
    "REVIEW_MAX_OUTPUT_TOKENS",
    "REVIEW_DIFF_TOKEN_BUDGET",
    "GATEKEEPER_ADMIN_TOKEN",
    "SESSION_SECRET",
    "HF_TOKEN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
]
SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "GEMINI_API_KEY",
    "GEMINI_API_KEYS",
    "GITHUB_TOKEN_DEV",
    "GITHUB_WEBHOOK_SECRET",
    "GATEKEEPER_ADMIN_TOKEN",
    "SESSION_SECRET",
    "HF_TOKEN",
}
OPTIONAL_KEYS = {
    "GEMINI_API_KEY",
    "GEMMA_MODEL_PRIMARY",
    "GEMMA_MODEL_SECONDARY",
    "HF_TOKEN",
    "MODEL_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "MODEL_MAX_CONCURRENCY",
    "MODEL_CACHE_TTL_S",
    "OLLAMA_NUM_CTX",
    "OLLAMA_KEEP_ALIVE",
    "GATEKEEPER_ALLOWED_REPOS",
    "GITHUB_API_BASE_URL",
    "WEBHOOK_MAX_BODY_BYTES",
    "JOB_TIMEOUT_S",
    "WORKER_POLL_INTERVAL_S",
    "JOB_LEASE_S",
    "JOB_MAX_ATTEMPTS",
    "DIFF_MAX_BYTES",
    "PR_MAX_FILES",
    "REVIEW_ABSTAIN_THRESHOLD",
    "REVIEW_NEGATIVE_MIN_CONFIDENCE",
    "REVIEW_MAX_EVIDENCE",
    "REVIEW_MAX_OUTPUT_TOKENS",
    "REVIEW_DIFF_TOKEN_BUDGET",
    "GEMINI_KEY_STRATEGY",
    "GEMINI_KEY_COOLDOWN_BASE_S",
    "GEMINI_KEY_COOLDOWN_MAX_S",
    "GEMINI_MAX_WAIT_FOR_KEY_S",
    "ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL",
}


def parse_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value
    return env


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copy(EXAMPLE, tmp_path / ".env.example")
    return tmp_path


def run(root: Path) -> int:
    return init_env.main(["--root", str(root)])


def test_template_has_exactly_the_documented_keys() -> None:
    assert init_env.parse_keys(EXAMPLE.read_text(encoding="utf-8")) == EXPECTED_KEYS


def test_template_documents_every_key_and_keeps_secrets_empty() -> None:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if not sep or key.startswith("#"):
            continue
        comment = lines[index - 1]
        assert comment.startswith("# "), f"{key} needs a one-line comment above it"
        assert "First needed:" in comment, f"{key} comment must say which prompt needs it"
        assert ("Optional" in comment) == (key in OPTIONAL_KEYS), key
        if key in SECRET_KEYS:
            assert value == "", f"{key} is secret and must stay empty in the template"


def test_creates_env_with_template_keys_in_order(root: Path) -> None:
    assert run(root) == 0
    assert list(parse_env(root / ".env")) == EXPECTED_KEYS


def test_sets_defaults_and_derives_database_url(root: Path) -> None:
    run(root)
    env = parse_env(root / ".env")
    for key, value in init_env.DEFAULTS.items():
        assert env[key] == value
    assert env["APP_ENV"] == "development"
    assert env["LOG_LEVEL"] == "INFO"
    assert env["MODEL_BACKEND"] == "ollama"
    assert env["OLLAMA_BASE_URL"] == "http://localhost:11434"
    password = env["POSTGRES_PASSWORD"]
    assert env["DATABASE_URL"] == f"postgresql://gatekeeper:{password}@127.0.0.1:15432/gatekeeper"


def test_generates_distinct_random_secrets(
    root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run(root)
    other = tmp_path_factory.mktemp("other")
    shutil.copy(EXAMPLE, other / ".env.example")
    run(other)
    first, second = parse_env(root / ".env"), parse_env(other / ".env")
    values = [first[key] for key in init_env.GENERATED_SECRETS]
    assert len(set(values)) == len(values)
    assert all(len(value) >= 43 for value in values)  # 32 random bytes, base64url
    assert all(first[key] != second[key] for key in init_env.GENERATED_SECRETS)


def test_leaves_every_other_key_empty_even_if_template_has_a_value(root: Path) -> None:
    template = root / ".env.example"
    text = template.read_text(encoding="utf-8").replace("OLLAMA_MODEL=\n", "OLLAMA_MODEL=x\n")
    template.write_text(text, encoding="utf-8")
    run(root)
    env = parse_env(root / ".env")
    managed = {*init_env.DEFAULTS, *init_env.GENERATED_SECRETS, *init_env.DERIVED}
    assert {key: value for key, value in env.items() if key not in managed} == dict.fromkeys(
        set(EXPECTED_KEYS) - managed, ""
    )


def test_prints_key_names_but_never_values(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(root)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    env = parse_env(root / ".env")
    for key in (*init_env.GENERATED_SECRETS, "DATABASE_URL"):
        assert key in output
        assert env[key] not in output


def test_refuses_to_overwrite_existing_env(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    existing = root / ".env"
    existing.write_text("KEEP=me\n", encoding="utf-8")
    assert run(root) == 1
    assert existing.read_text(encoding="utf-8") == "KEEP=me\n"
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_reports_missing_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path) == 2
    assert not (tmp_path / ".env").exists()
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("APP_ENV=development\n", "missing keys"),
        ("APP_ENV=a\nAPP_ENV=b\n", "duplicate keys"),
    ],
)
def test_rejects_unsafe_templates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], template: str, message: str
) -> None:
    (tmp_path / ".env.example").write_text(template, encoding="utf-8")
    assert run(tmp_path) == 2
    assert not (tmp_path / ".env").exists()
    assert message in capsys.readouterr().err

"""Tests for scripts/env_tools.py. They only touch temporary directories, never the real .env."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import env_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "value-that-must-never-be-printed-42"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def run(root: Path, command: str) -> int:
    return env_tools.main([command, "--root", str(root)])


def test_repo_template_assigns_every_key_to_a_prompt() -> None:
    keys = env_tools.parse_template((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    assert len(keys) == 30
    assert all(key.prompt != env_tools.UNASSIGNED for key in keys)
    assert {k.name for k in keys if k.optional} == {
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
    }
    assert {k.name for k in keys if k.prompt == "P01"} == {
        "APP_ENV",
        "LOG_LEVEL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "DATABASE_URL",
        "REDIS_URL",
    }


def test_parse_env_status_ignores_values_and_comments() -> None:
    text = f'A={SENTINEL}\nB=\nC=""\n# D=commented\nexport E = x\nnot a line\n'
    assert env_tools.parse_env_status(text) == {"A": True, "B": False, "C": False, "E": True}


def test_check_groups_by_prompt_and_never_prints_values(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / ".env").write_text(
        f"APP_ENV=development\nGITHUB_TOKEN_DEV={SENTINEL}\nEXTRA_KEY={SENTINEL}\n",
        encoding="utf-8",
    )
    assert run(root, "check") == 0
    out = capsys.readouterr().out
    assert SENTINEL not in out
    assert out.index("P01") < out.index("P02") < out.index("P03") < out.index("P16")
    assert "SET      APP_ENV" in out
    assert "SET      GITHUB_TOKEN_DEV" in out
    assert "MISSING  DATABASE_URL" in out
    assert "MISSING  HF_TOKEN  (optional)" in out
    assert "In .env but not in .env.example: EXTRA_KEY" in out
    assert "2 set, 28 missing (10 of them optional)." in out


def test_check_requires_env(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(root, "check") == 1
    assert "make init-env" in capsys.readouterr().err


def test_missing_template_is_an_error(tmp_path: Path) -> None:
    assert run(tmp_path, "check") == 2


def test_sync_appends_only_missing_keys_and_preserves_existing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = f"APP_ENV=development\nDATABASE_URL={SENTINEL}\nCUSTOM=1"  # no trailing newline
    env_file = root / ".env"
    env_file.write_text(original, encoding="utf-8")

    assert run(root, "sync") == 0
    out = capsys.readouterr().out
    assert SENTINEL not in out
    assert "Appended 28 key(s)" in out

    content = env_file.read_text(encoding="utf-8")
    assert content.startswith(original + "\n")
    status = env_tools.parse_env_status(content)
    assert status["DATABASE_URL"] is True
    assert status["HF_TOKEN"] is False
    assert len(status) == 31  # 30 template keys + CUSTOM

    assert run(root, "sync") == 0
    assert "nothing changed" in capsys.readouterr().out
    assert env_file.read_text(encoding="utf-8") == content


def test_template_without_prompt_comment_is_unassigned() -> None:
    (key,) = env_tools.parse_template("NO_COMMENT=1\n")
    assert key.prompt == env_tools.UNASSIGNED
    assert key.comment is None
    out = env_tools.check([key], {})
    assert "unassigned" in out

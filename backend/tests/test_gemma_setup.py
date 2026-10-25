"""gemma-setup against mocked Gemini model listings (respx). Synthetic keys only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from gatekeeper.config import Settings
from gatekeeper.models.gemma_setup import QUOTA_REMINDER, default_client_factory, pick_models, run

GOOD = "fake-good-key-" + "1" * 10
BAD = "fake-bad-key-" + "2" * 10
LIMITED = "fake-limited-key-" + "3" * 8
LIST = r"https://generativelanguage\.googleapis\.com/v1beta/models(\?.*)?$"


def listing(*names: str) -> dict[str, Any]:
    return {"models": [{"name": f"models/{n}"} for n in names]}


def error(code: int, status: str) -> dict[str, Any]:
    return {"error": {"code": code, "status": status, "message": "synthetic"}}


RESPONSES = {
    GOOD: httpx.Response(200, json=listing("gemma-4-26b-a4b-it", "gemma-4-31b-it", "gemini-x")),
    BAD: httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid. Please pass a valid API key.",
            }
        },
    ),
    LIMITED: httpx.Response(429, json=error(429, "RESOURCE_EXHAUSTED")),
}


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def respond(responses: dict[str, httpx.Response]) -> Any:
    return lambda request: responses[request.headers["x-goog-api-key"]]


def settings(keys: str) -> Settings:
    return Settings(_env_file=None, gemini_api_keys=keys)  # type: ignore[call-arg, arg-type]


@pytest.fixture
def env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("MODEL_BACKEND=ollama\nGEMMA_MODEL_PRIMARY=\n", encoding="utf-8")
    return path


def setup(keys: str, env: Path) -> int:
    s = settings(keys)
    return run(s, default_client_factory(s), env)


def test_missing_keys_print_an_action_block(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert setup("", env) == 2
    out = capsys.readouterr().out
    assert "=== ACTION NEEDED FROM USER ===" in out
    assert ".env key: GEMINI_API_KEYS" in out
    assert env.read_text(encoding="utf-8") == "MODEL_BACKEND=ollama\nGEMMA_MODEL_PRIMARY=\n"


def test_mixed_keys_table_and_settings(
    mock: respx.MockRouter, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(url__regex=LIST).mock(side_effect=respond(RESPONSES))
    assert setup(f"{BAD},{GOOD},{LIMITED}", env) == 0
    out = capsys.readouterr().out
    assert "key-1    invalid" in out
    assert "key-2    valid" in out
    assert "key-3    quota-limited" in out
    assert "Not usable: key-1, key-3" in out
    assert QUOTA_REMINDER in out
    for key in (GOOD, BAD, LIMITED):
        assert key not in out
    text = env.read_text(encoding="utf-8")
    assert "MODEL_BACKEND=gemini_api" in text
    assert "GEMMA_MODEL_PRIMARY=gemma-4-26b-a4b-it" in text
    assert "GEMMA_MODEL_SECONDARY=gemma-4-31b-it" in text
    assert GOOD not in text


def test_missing_secondary_model_is_reported(
    mock: respx.MockRouter, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    only_primary = {GOOD: httpx.Response(200, json=listing("gemma-4-26b-a4b-it"))}
    mock.get(url__regex=LIST).mock(side_effect=respond(only_primary))
    assert setup(GOOD, env) == 0
    out = capsys.readouterr().out
    assert "GEMMA_MODEL_SECONDARY left unchanged" in out
    assert "GEMMA_MODEL_SECONDARY" not in env.read_text(encoding="utf-8")


def test_all_keys_failing_asks_for_action(
    mock: respx.MockRouter, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get(url__regex=LIST).mock(side_effect=respond(RESPONSES))
    assert setup(BAD, env) == 1
    out = capsys.readouterr().out
    assert "=== ACTION NEEDED FROM USER ===" in out
    assert "Provide a working Gemini API key" in out
    assert "MODEL_BACKEND=ollama" in env.read_text(encoding="utf-8")


def test_pick_models() -> None:
    assert pick_models(["gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemma-3-1b"]) == (
        "gemma-4-26b-a4b-it",
        "gemma-4-31b-it",
        ["gemma-4-26b-a4b-it", "gemma-4-31b-it"],
    )
    assert pick_models(["gemini-x"]) == (None, None, [])

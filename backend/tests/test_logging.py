"""Tests for gatekeeper.logging."""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import structlog

from gatekeeper.config import Settings
from gatekeeper.logging import (
    bind_context,
    clear_context,
    configure_logging,
    current_context,
    get_logger,
    log_context,
)

SECRET = "known-secret-value-9c1e"
FAKE_TOKEN = "ghp_" + "Xy7" * 12  # synthetic


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    clear_context()
    structlog.reset_defaults()


def settings(app_env: str = "test") -> Settings:
    values: dict[str, Any] = {"app_env": app_env, "log_level": "DEBUG", "session_secret": SECRET}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def json_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_output_with_utc_iso_timestamp() -> None:
    stream = io.StringIO()
    configure_logging(settings("production"), stream=stream)
    get_logger("t").info("hello", count=2)
    (record,) = json_lines(stream)
    assert record["event"] == "hello"
    assert record["count"] == 2
    assert record["level"] == "info"
    assert record["logger"] == "t"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z", record["timestamp"])


def test_console_output_in_development() -> None:
    stream = io.StringIO()
    configure_logging(settings("development"), stream=stream)
    get_logger("t").info("readable", n=1)
    output = stream.getvalue()
    assert "readable" in output
    assert "n=1" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_masks_sensitive_keys_and_known_secrets() -> None:
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    get_logger("t").info(
        f"message mentions {SECRET}",
        api_key="abc",
        Authorization="Bearer whatever",
        session_cookie="c",
        db_password="p",
        github_token="t",
        nested={"client_secret": "s", "note": f"see {FAKE_TOKEN} and a@example.com"},
        plain="fine",
    )
    (record,) = json_lines(stream)
    assert record["event"] == "message mentions [REDACTED:secret]"
    for key in ("api_key", "Authorization", "session_cookie", "db_password", "github_token"):
        assert record[key] == "[REDACTED]"
    assert record["nested"] == {
        "client_secret": "[REDACTED]",
        "note": "see [REDACTED:github_token] and [REDACTED:email]",
    }
    assert record["plain"] == "fine"
    assert SECRET not in stream.getvalue()


def test_bodies_are_omitted_by_default() -> None:
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    get_logger().info("req", body="{...}", request_body="x", response_body="y")
    (record,) = json_lines(stream)
    assert record["body"] == record["request_body"] == record["response_body"] == "[OMITTED]"

    stream = io.StringIO()
    configure_logging(settings(), stream=stream, log_bodies=True)
    get_logger().info("req", body="visible")
    assert json_lines(stream)[0]["body"] == "visible"


def test_third_party_stdlib_logs_use_same_pipeline() -> None:
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    logging.getLogger("thirdparty.lib").warning("connecting with %s", FAKE_TOKEN)
    (record,) = json_lines(stream)
    assert record["event"] == "connecting with [REDACTED:github_token]"
    assert record["logger"] == "thirdparty.lib"
    assert record["level"] == "warning"


def test_exceptions_are_rendered_and_redacted() -> None:
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    try:
        raise RuntimeError(f"failed with {SECRET}")
    except RuntimeError:
        get_logger().exception("boom")
    (record,) = json_lines(stream)
    assert "RuntimeError: failed with [REDACTED:secret]" in record["exception"]


def test_context_binding() -> None:
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    run_id = uuid4()
    bind_context(request_id="req-1")
    with log_context(run_id=run_id, item_id=None):
        get_logger().info("inside")
    get_logger().info("outside")
    inside, outside = json_lines(stream)
    assert inside["request_id"] == "req-1"
    assert inside["run_id"] == str(run_id)
    assert "item_id" not in inside
    assert "run_id" not in outside
    assert current_context() == {"request_id": "req-1"}


def test_rejects_unknown_context_keys() -> None:
    with pytest.raises(ValueError, match="author"):
        bind_context(author="someone")


def test_reconfiguring_does_not_duplicate_handlers() -> None:
    configure_logging(settings(), stream=io.StringIO())
    configure_logging(settings(), stream=io.StringIO())
    names = [h.get_name() for h in logging.getLogger().handlers]
    assert names.count("gatekeeper") == 1

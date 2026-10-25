"""Gemini key pool: parsing, rotation, cooldowns, failover, shared cache, redaction.

All keys are synthetic and built at runtime (low entropy, so secret scanners stay quiet).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import fakeredis
import httpx
import pytest
import respx
import structlog
from pydantic import BaseModel, SecretStr, ValidationError

from gatekeeper.config import Settings
from gatekeeper.errors import GeminiKeysInvalid, ModelUnavailable
from gatekeeper.logging import configure_logging, get_logger
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.cache import RedisModelCache
from gatekeeper.models.gemini import GeminiBackend
from gatekeeper.models.key_pool import KeyPool
from gatekeeper.models.types import ModelRequest, TextPart
from gatekeeper.redaction import Redactor

KEY1 = "fake-key-one-" + "1" * 12
KEY2 = "fake-key-two-" + "2" * 12
KEY3 = "fake-key-three-" + "3" * 10
GOOGLE_SHAPED = "AIza" + "Q" * 35
MODEL = "gemma-4-26b-a4b-it"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def pool(*keys: str, clock: Clock | None = None, **options: Any) -> KeyPool:
    clock = clock or Clock()
    return KeyPool(
        [SecretStr(k) for k in keys],
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.0,
        **options,
    )


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {"model_backend": "gemini_api", "gemma_model_primary": MODEL}
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# -- parsing -------------------------------------------------------------------------------


def test_parsing_strips_dedupes_and_drops_empties() -> None:
    parsed = settings(gemini_api_keys=f" {KEY1} , {KEY2},, {KEY1}, ").gemini_keys()
    assert [k.get_secret_value() for k in parsed] == [KEY1, KEY2]


@pytest.mark.parametrize(("raw", "position"), [(f"{KEY1},bad key", 2), (f"'{KEY1}'", 1)])
def test_bad_entries_name_the_position_only(raw: str, position: int) -> None:
    with pytest.raises(ValidationError) as caught:
        settings(gemini_api_keys=raw)
    text = str(caught.value)
    assert f"entry {position}" in text
    assert KEY1 not in text
    assert "bad key" not in text


def test_too_many_keys() -> None:
    with pytest.raises(ValidationError, match="at most 20"):
        settings(gemini_api_keys=",".join(f"k{i:02d}" for i in range(21)))


def test_legacy_single_key_and_precedence() -> None:
    assert [k.get_secret_value() for k in settings(gemini_api_key=KEY3).gemini_keys()] == [KEY3]
    both = settings(gemini_api_key=KEY3, gemini_api_keys=f"{KEY1},{KEY2}")
    assert [k.get_secret_value() for k in both.gemini_keys()] == [KEY1, KEY2]
    assert settings(gemini_api_keys="").gemini_keys() == []
    assert set(both.secret_values()) >= {KEY1, KEY2, KEY3}


# -- selection -----------------------------------------------------------------------------


def test_round_robin_rotates_from_a_random_start() -> None:
    p = pool(KEY1, KEY2, KEY3)
    assert [p.acquire().alias for _ in range(4)] == ["key-1", "key-2", "key-3", "key-1"]
    later = KeyPool([SecretStr(k) for k in (KEY1, KEY2, KEY3)], rand=lambda: 0.5)
    assert later.acquire().alias == "key-2"


def test_primary_first_prefers_the_lowest_healthy_key() -> None:
    p = pool(KEY1, KEY2, strategy="primary_first")
    assert [p.acquire().alias for _ in range(3)] == ["key-1", "key-1", "key-1"]
    p.rate_limited("key-1")
    assert p.acquire().alias == "key-2"


def test_cooldown_backoff_expiry_and_reset() -> None:
    clock = Clock()
    p = pool(
        KEY1, KEY2, clock=clock, strategy="primary_first", cooldown_base_s=30, cooldown_max_s=100
    )
    assert p.rate_limited("key-1") == 30
    assert p.rate_limited("key-1") == 60
    assert p.rate_limited("key-1") == 100  # capped
    assert p.status()["key-1"] == "cooling"
    clock.now += 100
    assert p.acquire().alias == "key-1"
    p.success("key-1")
    assert p.rate_limited("key-1") == 30  # reset after success
    assert p.rate_limited("key-2", retry_after=7) == 7


def test_all_cooling_waits_when_short_else_raises_retryable() -> None:
    clock = Clock()
    p = pool(KEY1, KEY2, clock=clock, max_wait_s=60)
    p.rate_limited("key-1", retry_after=20)
    p.rate_limited("key-2", retry_after=10)
    assert p.acquire().alias == "key-2"
    assert clock.slept == [10]
    p.rate_limited("key-1", retry_after=500)
    p.rate_limited("key-2", retry_after=500)
    with pytest.raises(ModelUnavailable) as caught:
        p.acquire()
    assert caught.value.retryable
    assert caught.value.retry_after == pytest.approx(500, abs=20)


def test_invalid_keys_are_dropped_until_none_is_left() -> None:
    p = pool(KEY1, KEY2)
    p.invalid("key-1")
    assert {p.acquire().alias for _ in range(3)} == {"key-2"}
    p.invalid("key-2")
    with pytest.raises(GeminiKeysInvalid) as caught:
        p.acquire()
    assert (caught.value.code, caught.value.retryable) == ("gemini_keys_invalid", False)


def test_transient_error_moves_the_next_attempt_to_another_key() -> None:
    p = pool(KEY1, KEY2, strategy="primary_first")
    p.transient("key-1")
    assert p.acquire().alias == "key-2"
    assert p.acquire().alias == "key-1"


# -- driver failover (respx) ---------------------------------------------------------------


def reply(text: str = '{"ok": true}') -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    }


def error(code: int, status: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "status": status, "message": "synthetic", **extra}}


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf".*/models/{MODEL}$").respond(
            json={"name": f"models/{MODEL}", "inputTokenLimit": 262144}
        )
        yield router


def backend(*keys: str, clock: Clock | None = None, **options: Any) -> GeminiBackend:
    """primary_first by default so the order is deterministic even though the adapter's
    one-off context lookup (models.get) also takes a key."""
    options.setdefault("strategy", "primary_first")
    return GeminiBackend(
        settings(gemini_api_keys=",".join(keys)), pool=pool(*keys, clock=clock, **options)
    )


def by_key(responses: dict[str, httpx.Response]) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        return responses[request.headers["x-goog-api-key"]]

    return respond


class Ok(BaseModel):
    ok: bool


REQUEST = ModelRequest[Ok](
    prompt_id="t", prompt_version="1", parts=(TextPart(text="hi"),), response_model=Ok
)


@pytest.mark.parametrize(("headers", "cooldown"), [({"retry-after": "12"}, 12.0), ({}, 30.0)])
def test_429_fails_over_without_using_a_retry(
    mock: respx.MockRouter, headers: dict[str, str], cooldown: float
) -> None:
    clock = Clock()
    mock.post(url__regex=r".*:generateContent").mock(
        side_effect=by_key(
            {
                KEY1: httpx.Response(429, headers=headers, json=error(429, "RESOURCE_EXHAUSTED")),
                KEY2: httpx.Response(200, json=reply()),
            }
        )
    )
    driver = backend(KEY1, KEY2, clock=clock)
    response = ModelAdapter(driver, max_retries=0).generate(REQUEST, allow_unaudited=True)
    assert response.attempts == 1
    assert response.meta == {"key_alias": "key-2", "failovers": 1}
    assert driver.pool.status() == {"key-1": "cooling", "key-2": "healthy"}
    clock.now += cooldown - 0.5
    assert driver.pool.status()["key-1"] == "cooling"
    clock.now += 1
    assert driver.pool.status()["key-1"] == "healthy"


def test_every_key_rate_limited_is_retryable(mock: respx.MockRouter) -> None:
    mock.post(url__regex=r".*:generateContent").respond(429, json=error(429, "RESOURCE_EXHAUSTED"))
    with pytest.raises(ModelUnavailable) as caught:
        backend(KEY1, KEY2).with_key(lambda c: c.models.generate_content(model=MODEL, contents="x"))
    assert caught.value.retryable
    assert caught.value.details["failovers"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(401, json=error(401, "UNAUTHENTICATED")),
        httpx.Response(403, json=error(403, "PERMISSION_DENIED")),
        httpx.Response(
            400,
            json=error(400, "INVALID_ARGUMENT", details=[{"reason": "API_KEY_INVALID"}]),
        ),
    ],
)
def test_invalid_key_is_marked_dead_and_skipped(
    mock: respx.MockRouter, bad: httpx.Response
) -> None:
    mock.post(url__regex=r".*:generateContent").mock(
        side_effect=by_key({KEY1: bad, KEY2: httpx.Response(200, json=reply())})
    )
    driver = backend(KEY1, KEY2)
    _result, alias, failovers = driver.with_key(
        lambda c: c.models.generate_content(model=MODEL, contents="x")
    )
    assert (alias, failovers) == ("key-2", 1)
    assert driver.pool.status()["key-1"] == "dead"


def test_all_keys_invalid(mock: respx.MockRouter) -> None:
    mock.post(url__regex=r".*:generateContent").respond(401, json=error(401, "UNAUTHENTICATED"))
    with pytest.raises(GeminiKeysInvalid) as caught:
        backend(KEY1, KEY2).with_key(lambda c: c.models.generate_content(model=MODEL, contents="x"))
    assert KEY1 not in str(caught.value)
    assert KEY2 not in str(caught.value)


def test_cache_is_shared_across_keys(mock: respx.MockRouter) -> None:
    route = mock.post(url__regex=r".*:generateContent").respond(json=reply())
    redis = fakeredis.FakeRedis(decode_responses=True)
    driver = backend(KEY1, KEY2)
    adapter = ModelAdapter(driver, cache=RedisModelCache(redis, ttl_s=60))
    first = adapter.generate(REQUEST, allow_unaudited=True)
    second = adapter.generate(REQUEST, allow_unaudited=True)
    assert (first.cache_hit, first.meta["key_alias"]) == (False, "key-1")
    assert second.cache_hit is True
    assert route.call_count == 1
    stored = json.dumps({k: redis.get(k) for k in redis.keys("*")})
    assert KEY1 not in stored
    assert KEY2 not in stored


# -- redaction -----------------------------------------------------------------------------


def test_redactor_masks_every_pool_key_and_google_shaped_keys() -> None:
    redactor = Redactor.from_settings(
        settings(gemini_api_keys=f"{KEY1},{KEY2}", gemini_api_key=KEY3)
    )
    text = f"keys {KEY1} {KEY2} {KEY3} and {GOOGLE_SHAPED}."
    masked = redactor.redact_text(text)
    for key in (KEY1, KEY2, KEY3, GOOGLE_SHAPED):
        assert key not in masked
    assert "[REDACTED:google_api_key]" in masked
    assert redactor.redact({"error": f"bad {KEY2}"}) == {"error": "bad [REDACTED:secret]"}
    assert Redactor().redact_text("AIza" + "Q" * 36) == "AIza" + "Q" * 36  # wrong length


def test_logging_masks_pool_keys() -> None:
    stream = io.StringIO()
    configure_logging(settings(app_env="test", gemini_api_keys=f"{KEY1},{KEY2}"), stream=stream)
    try:
        get_logger("t").warning("failure", detail=f"{KEY2} rejected", other=GOOGLE_SHAPED)
    finally:
        logging.getLogger().handlers[:] = [
            h for h in logging.getLogger().handlers if h.get_name() != "gatekeeper"
        ]
        structlog.reset_defaults()
    output = stream.getvalue()
    assert KEY2 not in output
    assert GOOGLE_SHAPED not in output

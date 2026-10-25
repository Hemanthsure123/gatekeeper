"""Unit tests for the model adapter core (FakeBackend, fakeredis; no network, no database)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

import fakeredis
import pytest
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from gatekeeper.audit import Actor, EventType, Run
from gatekeeper.errors import (
    AuditError,
    CapabilityMissing,
    ConfigError,
    ExternalServiceError,
    InputTooLarge,
    ModelOutputInvalid,
    ModelTimeout,
    ModelUnavailable,
    PolicyViolation,
)
from gatekeeper.models.adapter import ModelAdapter, compose_system, estimate_tokens
from gatekeeper.models.backend import BackendCall, Completion
from gatekeeper.models.cache import RedisModelCache, cache_key
from gatekeeper.models.testing import DEFAULT_CAPABILITIES, FakeBackend, fake_adapter
from gatekeeper.models.types import (
    CachePolicy,
    Capabilities,
    ImagePart,
    ModelRequest,
    ModelRole,
    Reasoning,
    TextPart,
)


class Greeting(BaseModel):
    greeting: str
    number: int


GOOD = Greeting(greeting="hello", number=7)
PNG = ImagePart.from_bytes(b"\x89PNG\r\n\x1a\nfake", "image/png")


def make_request(**overrides: Any) -> ModelRequest[Greeting]:
    values: dict[str, Any] = {
        "prompt_id": "test.greeting",
        "prompt_version": "1",
        "system": "Be brief.",
        "parts": [TextPart(text="Say hello.")],
        "response_model": Greeting,
    }
    values.update(overrides)
    return ModelRequest[Greeting].model_validate(values)


@dataclass
class FakeEvent:
    seq: int


@dataclass
class FakeRun:
    """Duck-typed audit Run that keeps events in memory."""

    fail_on: EventType | None = None
    events: list[tuple[EventType, Actor, dict[str, Any], int | None]] = field(default_factory=list)

    def record(
        self,
        event_type: EventType,
        actor: Actor,
        payload: dict[str, Any],
        parent_seq: int | None = None,
    ) -> FakeEvent:
        if event_type is self.fail_on:
            raise AuditError("could not write audit event")
        self.events.append((event_type, actor, payload, parent_seq))
        return FakeEvent(seq=len(self.events))


def as_run(fake: FakeRun) -> Run:
    return cast(Run, fake)


def redis_cache() -> RedisModelCache:
    return RedisModelCache(fakeredis.FakeRedis(decode_responses=True), ttl_s=60)


# -- policy and validation ---------------------------------------------------------------


def test_refuses_unaudited_calls_unless_explicit() -> None:
    adapter, fake = fake_adapter([GOOD])
    with pytest.raises(PolicyViolation, match="audited"):
        adapter.generate(make_request())
    assert fake.calls == []
    assert adapter.generate(make_request(), allow_unaudited=True).parsed == GOOD


def test_validators_can_block_requests() -> None:
    def no_secrets(request: ModelRequest[Any]) -> None:
        if any("password" in text for text in request.text_parts):
            raise PolicyViolation("blocked")

    adapter, fake = fake_adapter([GOOD], validators=[no_secrets])
    with pytest.raises(PolicyViolation, match="blocked"):
        adapter.generate(make_request(parts=[TextPart(text="my password")]), allow_unaudited=True)
    assert fake.calls == []
    adapter.add_validator(lambda _request: None)
    assert adapter.generate(make_request(), allow_unaudited=True).attempts == 1


def test_happy_path_sends_schema_natively_and_in_system_text() -> None:
    adapter, fake = fake_adapter([GOOD])
    response = adapter.generate(make_request(reasoning=Reasoning.ON), allow_unaudited=True)
    assert response.parsed == GOOD
    assert (response.backend, response.model, response.cache_hit) == ("fake", "fake-primary", False)
    (call,) = fake.calls
    assert call.json_schema == Greeting.model_json_schema()
    assert call.system.startswith("Be brief.")
    assert '"greeting"' in call.system
    assert call.reasoning is Reasoning.ON
    assert call.temperature == 0


def test_secondary_role_requires_configuration() -> None:
    adapter, _ = fake_adapter([GOOD], models={ModelRole.PRIMARY: "p"})
    with pytest.raises(ConfigError, match="secondary"):
        adapter.generate(make_request(role=ModelRole.SECONDARY), allow_unaudited=True)


def test_images_need_capability() -> None:
    no_images = Capabilities(False, True, True, 8192)
    adapter, fake = fake_adapter([GOOD], capabilities=no_images)
    with pytest.raises(CapabilityMissing):
        adapter.generate(make_request(parts=[TextPart(text="x"), PNG]), allow_unaudited=True)
    assert fake.calls == []


# -- retries -------------------------------------------------------------------------------


def adapter_with_clock(
    responses: list[Any], max_retries: int = 2
) -> tuple[ModelAdapter, FakeBackend, list[float]]:
    sleeps: list[float] = []
    backend = FakeBackend(responses)
    adapter = ModelAdapter(backend, max_retries=max_retries, sleep=sleeps.append, rand=lambda: 0.0)
    return adapter, backend, sleeps


def test_retries_retryable_errors_with_exponential_backoff() -> None:
    adapter, fake, sleeps = adapter_with_clock(
        [ModelUnavailable("down"), ModelTimeout("slow"), GOOD]
    )
    response = adapter.generate(make_request(), allow_unaudited=True)
    assert response.attempts == 3
    assert len(fake.calls) == 3
    assert sleeps == [0.5, 1.0]  # ceiling 1s then 2s, jitter fixed at the low end


def test_backoff_jitter_upper_bound() -> None:
    sleeps: list[float] = []
    adapter = ModelAdapter(
        FakeBackend([ModelUnavailable("x"), GOOD]), sleep=sleeps.append, rand=lambda: 1.0
    )
    adapter.generate(make_request(), allow_unaudited=True)
    assert sleeps == [1.0]


def test_honours_retry_after() -> None:
    adapter, _, sleeps = adapter_with_clock([ModelUnavailable("429", retry_after=7.0), GOOD])
    adapter.generate(make_request(), allow_unaudited=True)
    assert sleeps == [7.0]


def test_gives_up_after_max_retries() -> None:
    adapter, fake, sleeps = adapter_with_clock([ModelUnavailable(str(i)) for i in range(5)])
    with pytest.raises(ModelUnavailable, match="2"):
        adapter.generate(make_request(), allow_unaudited=True)
    assert len(fake.calls) == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "error",
    [
        ExternalServiceError("400 bad request", retryable=False),
        ModelUnavailable("404 model not found", retryable=False),
    ],
)
def test_never_retries_non_retryable_errors(error: Exception) -> None:
    adapter, fake, sleeps = adapter_with_clock([error, GOOD])
    with pytest.raises(type(error)):
        adapter.generate(make_request(), allow_unaudited=True)
    assert len(fake.calls) == 1
    assert sleeps == []


# -- structured output and repair ----------------------------------------------------------


def test_one_repair_attempt_then_success() -> None:
    adapter, fake = fake_adapter(['{"greeting": "hi"}', GOOD])
    response = adapter.generate(make_request(), allow_unaudited=True)
    assert response.parsed == GOOD
    assert response.attempts == 2
    first, repair = fake.calls
    assert first.followups == ()
    assistant, user = repair.followups
    assert (assistant.role, assistant.text) == ("assistant", '{"greeting": "hi"}')
    assert user.role == "user"
    assert "number: Field required" in user.text


def test_free_text_is_not_parsed_and_raises_after_repair() -> None:
    adapter, fake = fake_adapter(['Sure! {"greeting": "hi", "number": 1}', "still not json"])
    with pytest.raises(ModelOutputInvalid) as caught:
        adapter.generate(make_request(), allow_unaudited=True)
    assert len(fake.calls) == 2
    assert caught.value.retryable is False
    assert "Invalid JSON" in caught.value.details["problem"]


# -- input size ----------------------------------------------------------------------------


def test_input_too_large_before_sending() -> None:
    small = Capabilities(True, True, True, max_context_tokens=2048)
    adapter, fake = fake_adapter([GOOD], capabilities=small)
    big = "x" * 3 * 2048
    with pytest.raises(InputTooLarge, match="tokens"):
        adapter.generate(make_request(parts=[TextPart(text=big)]), allow_unaudited=True)
    assert fake.calls == []


def test_output_budget_counts_against_context() -> None:
    small = Capabilities(True, True, True, max_context_tokens=2048)
    adapter, _ = fake_adapter([GOOD], capabilities=small)
    with pytest.raises(InputTooLarge):
        adapter.generate(make_request(max_output_tokens=2000), allow_unaudited=True)


def test_reported_prompt_tokens_at_limit_mean_possible_truncation() -> None:
    caps = Capabilities(True, True, True, max_context_tokens=4096)
    truncated = Completion(text=GOOD.model_dump_json(), input_tokens=4096 - 1024, output_tokens=9)
    adapter, _ = fake_adapter([truncated], capabilities=caps)
    with pytest.raises(InputTooLarge, match="truncated"):
        adapter.generate(make_request(), allow_unaudited=True)


def test_size_check_skipped_when_context_unknown() -> None:
    unknown = Capabilities(True, True, True, max_context_tokens=None)
    adapter, _ = fake_adapter([GOOD], capabilities=unknown)
    huge = "x" * 100_000
    assert adapter.generate(make_request(parts=[TextPart(text=huge)]), allow_unaudited=True)


def test_estimate_tokens_counts_utf8_bytes_and_images() -> None:
    assert estimate_tokens(["abc"]) == 1 + 64
    assert estimate_tokens(["日本"], image_count=1) == 2 + 1024 + 64


# -- cache ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"system": "Be verbose."},
        {"parts": [TextPart(text="Say bye.")]},
        {"parts": [TextPart(text="Say hello."), PNG]},
        {"temperature": 0.5},
        {"seed": 1},
        {"reasoning": Reasoning.ON},
        {"prompt_id": "other"},
        {"prompt_version": "2"},
    ],
)
def test_cache_key_changes_with_each_input(change: dict[str, Any]) -> None:
    schema = Greeting.model_json_schema()
    base = cache_key(backend="b", model="m", request=make_request(), json_schema=schema)
    assert (
        cache_key(backend="b", model="m", request=make_request(**change), json_schema=schema)
        != base
    )


def test_cache_key_covers_backend_model_and_schema() -> None:
    request, schema = make_request(), Greeting.model_json_schema()
    base = cache_key(backend="b", model="m", request=request, json_schema=schema)
    assert cache_key(backend="c", model="m", request=request, json_schema=schema) != base
    assert cache_key(backend="b", model="n", request=request, json_schema=schema) != base
    assert cache_key(backend="b", model="m", request=request, json_schema={}) != base


def test_cache_policy_use_reads_and_writes() -> None:
    adapter, fake = fake_adapter([GOOD], cache=redis_cache())
    first = adapter.generate(make_request(), allow_unaudited=True)
    second = adapter.generate(make_request(), allow_unaudited=True)
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert second.parsed == GOOD
    assert second.attempts == 0
    assert len(fake.calls) == 1


def test_cache_policy_refresh_writes_only_and_bypass_does_neither() -> None:
    cache = redis_cache()
    other = Greeting(greeting="fresh", number=1)
    adapter, fake = fake_adapter([GOOD, other, GOOD], cache=cache)
    adapter.generate(make_request(), allow_unaudited=True)
    refreshed = adapter.generate(
        make_request(cache_policy=CachePolicy.REFRESH), allow_unaudited=True
    )
    assert refreshed.cache_hit is False
    assert adapter.generate(make_request(), allow_unaudited=True).parsed == other
    bypass = adapter.generate(make_request(cache_policy=CachePolicy.BYPASS), allow_unaudited=True)
    assert bypass.cache_hit is False
    assert adapter.generate(make_request(), allow_unaudited=True).parsed == other
    assert len(fake.calls) == 3


def test_failures_are_never_cached() -> None:
    cache = redis_cache()
    adapter, fake = fake_adapter(["not json", "still not json", GOOD], cache=cache)
    with pytest.raises(ModelOutputInvalid):
        adapter.generate(make_request(), allow_unaudited=True)
    assert adapter.generate(make_request(), allow_unaudited=True).cache_hit is False
    assert len(fake.calls) == 3


class BrokenRedis:
    def get(self, *_args: object) -> None:
        raise RedisConnectionError("down")

    def set(self, *_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("down")


def test_cache_fails_open() -> None:
    cache = RedisModelCache(cast(Any, BrokenRedis()), ttl_s=60)
    adapter, _ = fake_adapter([GOOD, GOOD], cache=cache)
    assert adapter.generate(make_request(), allow_unaudited=True).parsed == GOOD
    assert adapter.generate(make_request(), allow_unaudited=True).cache_hit is False


def test_unreadable_cache_entry_is_a_miss() -> None:
    cache = redis_cache()
    adapter, fake = fake_adapter([GOOD], cache=cache)
    key = cache_key(
        backend="fake",
        model="fake-primary",
        request=make_request(),
        json_schema=Greeting.model_json_schema(),
    )
    cache.set(key, json.dumps({"v": 1, "parsed": {"greeting": 1}}))
    assert adapter.generate(make_request(), allow_unaudited=True).cache_hit is False
    assert len(fake.calls) == 1


# -- audit ---------------------------------------------------------------------------------


def test_audit_records_request_and_response_with_image_hashes_only() -> None:
    run = FakeRun()
    adapter, _ = fake_adapter([GOOD])
    adapter.generate(make_request(parts=[TextPart(text="look"), PNG]), as_run(run))
    (req_type, req_actor, req, _), (resp_type, resp_actor, resp, parent) = run.events
    assert (req_type, req_actor) == (EventType.MODEL_REQUEST, Actor.SYSTEM)
    assert (resp_type, resp_actor, parent) == (EventType.MODEL_RESPONSE, Actor.MODEL, 1)
    assert req["prompt_id"] == "test.greeting"
    assert req["text_parts"] == ["look"]
    assert req["images"] == [
        {"sha256": PNG.sha256, "mime_type": "image/png", "size_bytes": len(PNG.data)}
    ]
    assert "fake" not in json.dumps(req["images"])  # no image bytes anywhere
    assert req["settings"]["temperature"] == 0
    assert resp["parsed"] == {"greeting": "hello", "number": 7}
    assert resp["cache_hit"] is False


def test_audit_failure_before_the_call_aborts_it() -> None:
    adapter, fake = fake_adapter([GOOD])
    with pytest.raises(AuditError):
        adapter.generate(make_request(), as_run(FakeRun(fail_on=EventType.MODEL_REQUEST)))
    assert fake.calls == []


def test_audit_failure_after_the_call_aborts_and_skips_cache() -> None:
    cache = redis_cache()
    adapter, _ = fake_adapter([GOOD, GOOD], cache=cache)
    with pytest.raises(AuditError):
        adapter.generate(make_request(), as_run(FakeRun(fail_on=EventType.MODEL_RESPONSE)))
    assert adapter.generate(make_request(), allow_unaudited=True).cache_hit is False


def test_model_failures_are_audited_as_errors() -> None:
    run = FakeRun()
    adapter, _ = fake_adapter([ExternalServiceError("bad", retryable=False)])
    with pytest.raises(ExternalServiceError):
        adapter.generate(make_request(), as_run(run))
    assert [event[0] for event in run.events] == [EventType.MODEL_REQUEST, EventType.ERROR]
    assert run.events[1][2]["code"] == "external_service_error"
    assert run.events[1][3] == 1


def test_fake_backend_reports_exhaustion_and_supports_callables() -> None:
    fake = FakeBackend(lambda call: {"greeting": call.model, "number": 1})
    adapter = ModelAdapter(fake)
    assert adapter.generate(make_request(), allow_unaudited=True).parsed.greeting == "fake-primary"
    empty = FakeBackend()
    with pytest.raises(AssertionError, match="no scripted response"):
        empty.complete(cast(BackendCall, None))
    assert DEFAULT_CAPABILITIES.max_context_tokens == 8192
    assert compose_system("", {}).startswith("Reply with")


def test_logs_estimate_next_to_reported_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from gatekeeper.models import adapter as adapter_module

    seen: list[dict[str, Any]] = []

    class Recorder:
        def info(self, _event: str, **fields: Any) -> None:
            seen.append(fields)

        def warning(self, _event: str, **_fields: Any) -> None:
            """Not under test."""

    monkeypatch.setattr(adapter_module, "log", Recorder())
    completion = Completion(text=GOOD.model_dump_json(), input_tokens=120, output_tokens=9)
    adapter, _ = fake_adapter([completion])
    adapter.generate(make_request(), allow_unaudited=True)
    (fields,) = seen
    assert fields["input_tokens"] == 120
    assert fields["estimated_input_tokens"] > 0


def test_audit_records_backend_meta_without_secrets() -> None:
    run = FakeRun()
    meta = {"key_alias": "key-2", "failovers": 1}
    adapter, _ = fake_adapter([Completion(text=GOOD.model_dump_json(), meta=meta)])
    response = adapter.generate(make_request(), as_run(run))
    assert response.meta == meta
    assert run.events[1][2]["backend_meta"] == meta

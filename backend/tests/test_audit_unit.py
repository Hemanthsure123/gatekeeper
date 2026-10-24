"""Unit tests for audit hashing, chain verification and fail-closed recording (no database)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import (
    Actor,
    AuditRecorder,
    EventType,
    Redactor,
    StoredEvent,
    canonical_json,
    chain_hash,
    format_timestamp,
    payload_hash,
    verify_events,
)
from gatekeeper.db.models import EVENT_ACTORS
from gatekeeper.errors import AuditError


def test_canonical_json_is_sorted_compact_utf8() -> None:
    assert canonical_json({"b": 1, "a": [1, 2, {"d": "é", "c": None}]}) == (
        '{"a":[1,2,{"c":null,"d":"é"}],"b":1}'.encode()
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1, 2}, object()])
def test_canonical_json_rejects_non_json(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"x": bad})


def test_payload_hash_is_sha256_of_canonical_json() -> None:
    expected = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert payload_hash({"b": 2, "a": 1}) == expected


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("00000000-0000-0000-0000-0000000000aa")
WHEN = datetime(2026, 10, 25, 9, 30, 0, 123, tzinfo=UTC)


def link(previous: str | None = None, **overrides: Any) -> str:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "seq": 2,
        "event_type": "verdict",
        "payload_digest": "abc",
        "actor": "model",
        "parent_seq": 1,
        "item_id": ITEM_ID,
        "schema_version": "gatekeeper.event/1",
        "created_at": WHEN,
    }
    fields.update(overrides)
    return chain_hash(previous, **fields)


def test_format_timestamp_is_fixed_utc_with_microseconds() -> None:
    assert format_timestamp(WHEN) == "2026-10-25T09:30:00.000123Z"
    plus_two = timezone(timedelta(hours=2))
    assert format_timestamp(WHEN.astimezone(plus_two)) == "2026-10-25T09:30:00.000123Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        format_timestamp(datetime(2026, 1, 1))  # deliberately naive


def test_chain_hash_formula() -> None:
    preimage = (
        '{"actor":"model","created_at":"2026-10-25T09:30:00.000123Z",'
        f'"item_id":"{ITEM_ID}","parent_seq":1,"payload_hash":"abc","previous":"p",'
        f'"run_id":"{RUN_ID}","schema_version":"gatekeeper.event/1","seq":2,"type":"verdict"}}'
    ).encode()
    assert link("p") == hashlib.sha256(preimage).hexdigest()
    assert link("") == link(None)


@pytest.mark.parametrize(
    "override",
    [
        {"actor": "maintainer"},
        {"parent_seq": None},
        {"item_id": None},
        {"schema_version": "gatekeeper.event/2"},
        {"created_at": WHEN + timedelta(microseconds=1)},
        {"seq": 3},
        {"event_type": "decision"},
        {"payload_digest": "abd"},
        {"run_id": UUID(int=2)},
    ],
)
def test_chain_hash_covers_every_field(override: dict[str, Any]) -> None:
    assert link("p", **override) != link("p")


def test_enums_are_stable() -> None:
    assert [e.value for e in EventType] == [
        "run_start",
        "run_end",
        "intake",
        "signal_result",
        "model_request",
        "model_response",
        "verifier_result",
        "verdict",
        "decision",
        "action",
        "error",
    ]
    assert tuple(actor.value for actor in Actor) == EVENT_ACTORS


def build_chain(run_id: UUID, payloads: list[dict[str, Any]]) -> list[StoredEvent]:
    events: list[StoredEvent] = []
    previous: str | None = None
    for seq, payload in enumerate(payloads, start=1):
        digest = payload_hash(payload)
        created_at = datetime.now(UTC)
        current = chain_hash(
            previous,
            run_id=run_id,
            seq=seq,
            event_type="intake",
            payload_digest=digest,
            actor="system",
            parent_seq=None,
            item_id=None,
            schema_version="gatekeeper.event/1",
            created_at=created_at,
        )
        events.append(
            StoredEvent(
                id=seq,
                run_id=run_id,
                seq=seq,
                parent_seq=None,
                item_id=None,
                type="intake",
                actor="system",
                payload=payload,
                payload_hash=digest,
                chain_hash=current,
                schema_version="gatekeeper.event/1",
                created_at=created_at,
            )
        )
        previous = current
    return events


def test_verify_events_accepts_a_valid_chain() -> None:
    run_id = uuid4()
    result = verify_events(run_id, build_chain(run_id, [{"a": 1}, {"b": 2}, {"c": 3}]))
    assert result.ok
    assert result.checked == 3
    assert result.first_bad_seq is None


def test_verify_events_detects_forged_payload() -> None:
    run_id = uuid4()
    events = build_chain(run_id, [{"a": 1}, {"b": 2}, {"c": 3}])
    events[1] = replace(events[1], payload={"b": 999})
    result = verify_events(run_id, events)
    assert (result.ok, result.first_bad_seq, result.reason) == (False, 2, "payload_hash mismatch")


def test_verify_events_detects_consistently_rehashed_forgery() -> None:
    run_id = uuid4()
    events = build_chain(run_id, [{"a": 1}, {"b": 2}, {"c": 3}])
    forged = {"b": 999}
    events[1] = replace(events[1], payload=forged, payload_hash=payload_hash(forged))
    result = verify_events(run_id, events)
    assert (result.ok, result.first_bad_seq, result.reason) == (False, 2, "chain_hash mismatch")


def test_verify_events_detects_forged_actor() -> None:
    run_id = uuid4()
    events = build_chain(run_id, [{"a": 1}, {"b": 2}])
    events[1] = replace(events[1], actor="maintainer")
    result = verify_events(run_id, events)
    assert (result.ok, result.first_bad_seq, result.reason) == (False, 2, "chain_hash mismatch")


def test_verify_events_detects_gaps_and_foreign_events() -> None:
    run_id = uuid4()
    events = build_chain(run_id, [{"a": 1}, {"b": 2}, {"c": 3}])
    gap = verify_events(run_id, [events[0], events[2]])
    assert (gap.ok, gap.first_bad_seq) == (False, 2)
    other = verify_events(uuid4(), events)
    assert (other.ok, other.reason) == (False, "event belongs to another run")
    assert verify_events(run_id, []).reason == "run has no events"


class _BrokenFactory:
    def begin(self) -> Session:
        raise OperationalError("INSERT INTO events", {}, ConnectionError("connection lost"))


def test_recorder_fails_closed_when_the_write_fails() -> None:
    recorder = AuditRecorder(cast(sessionmaker[Session], _BrokenFactory()), Redactor())
    with pytest.raises(AuditError) as caught:
        recorder.start_run()
    assert caught.value.code == "audit_error"
    assert caught.value.details["seq"] == 1
    assert "could not write" in caught.value.message

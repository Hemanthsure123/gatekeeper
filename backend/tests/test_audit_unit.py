"""Unit tests for audit hashing, chain verification and fail-closed recording (no database)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
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


def test_chain_hash_formula() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    preimage = f'["","{run_id}",1,"run_start","abc"]'.encode()
    assert chain_hash(None, run_id, 1, "run_start", "abc") == hashlib.sha256(preimage).hexdigest()
    assert chain_hash("", run_id, 1, "run_start", "abc") == chain_hash(
        None, run_id, 1, "run_start", "abc"
    )
    assert chain_hash("p", run_id, 2, "verdict", "abc") != chain_hash(
        "q", run_id, 2, "verdict", "abc"
    )


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
        link = chain_hash(previous, run_id, seq, "intake", digest)
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
                chain_hash=link,
                schema_version="gatekeeper.event/1",
                created_at=datetime.now(UTC),
            )
        )
        previous = link
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

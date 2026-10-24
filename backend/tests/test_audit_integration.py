"""Integration tests for the audit trail. Need Postgres (see conftest.py)."""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import (
    Actor,
    AuditReader,
    AuditRecorder,
    EventType,
    Redactor,
    Run,
    payload_hash,
)
from gatekeeper.db import Item, Repo, session_scope
from gatekeeper.errors import AuditError
from gatekeeper.models.testing import fake_adapter
from gatekeeper.models.types import ImagePart, ModelRequest, TextPart

pytestmark = pytest.mark.integration

SECRET = "integration-secret-value-31"


@pytest.fixture
def recorder(session_factory: sessionmaker[Session]) -> AuditRecorder:
    return AuditRecorder(session_factory, Redactor([SECRET]))


@pytest.fixture
def reader(session_factory: sessionmaker[Session]) -> AuditReader:
    return AuditReader(session_factory)


@pytest.fixture
def item_id(session_factory: sessionmaker[Session]) -> UUID:
    with session_scope(session_factory) as session:
        repo = Repo(owner="octo", name=f"audit-{uuid.uuid4().hex[:8]}")
        session.add(repo)
        session.flush()
        item = Item(
            repo_id=repo.id,
            kind="pr",
            external_id="1",
            content={},
            content_hash="0" * 64,
            state="open",
        )
        session.add(item)
        session.flush()
        return item.id


def full_run(recorder: AuditRecorder, item_id: UUID | None = None) -> Run:
    run = recorder.start_run(item_id=item_id, payload={"trigger": "test"})
    intake = run.record(EventType.INTAKE, Actor.GITHUB, {"title": "Fix parser"})
    run.record(EventType.MODEL_REQUEST, Actor.SYSTEM, {"prompt": "p1"}, parent_seq=intake.seq)
    run.record(EventType.VERDICT, Actor.MODEL, {"label": "looks_valuable", "confidence": 0.8})
    run.end()
    return run


def test_records_a_verifiable_run(
    recorder: AuditRecorder, reader: AuditReader, item_id: UUID
) -> None:
    run = full_run(recorder, item_id)
    events = reader.get_events(run.run_id)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert [e.type for e in events] == [
        "run_start",
        "intake",
        "model_request",
        "verdict",
        "run_end",
    ]
    assert events[2].parent_seq == 2
    assert all(e.item_id == item_id for e in events)
    assert all(e.schema_version == "gatekeeper.event/1" for e in events)
    assert events[-1].payload == {"status": "completed"}
    result = reader.verify_chain(run.run_id)
    assert result.ok
    assert result.checked == 5


def test_payload_is_redacted_before_hashing_and_storage(
    recorder: AuditRecorder, reader: AuditReader
) -> None:
    run = recorder.start_run()
    stored = run.record(
        EventType.ERROR, Actor.SYSTEM, {"detail": f"token {SECRET} from dev@example.com"}
    )
    expected = {"detail": "token [REDACTED:secret] from [REDACTED:email]"}
    assert stored.payload == expected
    assert stored.payload_hash == payload_hash(expected)
    assert reader.get_events(run.run_id)[1].payload == expected


def test_ended_and_invalid_runs_refuse_writes(recorder: AuditRecorder) -> None:
    run = full_run(recorder)
    assert run.ended
    with pytest.raises(AuditError, match="already ended"):
        run.record(EventType.ACTION, Actor.SYSTEM, {})

    fresh = recorder.start_run()
    with pytest.raises(AuditError, match="parent_seq"):
        fresh.record(EventType.ACTION, Actor.SYSTEM, {}, parent_seq=5)


def test_unserializable_payload_fails_closed(recorder: AuditRecorder) -> None:
    run = recorder.start_run()
    with pytest.raises(AuditError, match="JSON"):
        run.record(EventType.INTAKE, Actor.SYSTEM, {"when": object()})
    with pytest.raises(AuditError, match="failed state"):
        run.record(EventType.INTAKE, Actor.SYSTEM, {"ok": True})


def test_second_writer_is_stopped_by_unique_constraint(recorder: AuditRecorder) -> None:
    run = recorder.start_run()
    impostor = Run(recorder, run_id=run.run_id, item_id=None)
    with pytest.raises(AuditError, match="could not write"):
        impostor.record(EventType.RUN_START, Actor.SYSTEM, {})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET payload = '{}' WHERE run_id = :run",
        "DELETE FROM events WHERE run_id = :run",
        "TRUNCATE events",
    ],
)
def test_trigger_rejects_mutation(
    recorder: AuditRecorder, session_factory: sessionmaker[Session], statement: str
) -> None:
    run = full_run(recorder)
    with pytest.raises(DBAPIError, match="append-only"), session_scope(session_factory) as session:
        session.execute(text(statement), {"run": run.run_id})


def test_verify_chain_detects_a_forged_row(
    recorder: AuditRecorder, reader: AuditReader, migrated_engine: Engine
) -> None:
    run = full_run(recorder)
    forged = '{"label": "likely_low_value", "confidence": 0.99}'
    # Simulate an attacker with superuser rights who bypasses the trigger.
    with migrated_engine.begin() as connection:
        connection.execute(text("ALTER TABLE events DISABLE TRIGGER events_append_only"))
        connection.execute(
            text("UPDATE events SET payload = CAST(:p AS jsonb) WHERE run_id = :r AND seq = 4"),
            {"p": forged, "r": run.run_id},
        )
        connection.execute(text("ALTER TABLE events ENABLE TRIGGER events_append_only"))

    result = reader.verify_chain(run.run_id)
    assert (result.ok, result.first_bad_seq, result.reason) == (False, 4, "payload_hash mismatch")
    assert result.checked == 3


def test_verify_unknown_run(reader: AuditReader) -> None:
    result = reader.verify_chain(uuid.uuid4())
    assert not result.ok
    assert result.reason == "run has no events"


class _Echo(BaseModel):
    answer: str


def test_model_calls_are_audited_into_a_verifiable_chain(
    recorder: AuditRecorder, reader: AuditReader
) -> None:
    image = ImagePart.from_bytes(b"\x89PNG-fake-image-bytes", "image/png")
    adapter, _ = fake_adapter([_Echo(answer="ok")])
    run = recorder.start_run()
    request = ModelRequest[_Echo](
        prompt_id="test.echo",
        prompt_version="1",
        parts=(TextPart(text=f"mail dev@example.com {SECRET}"), image),
        response_model=_Echo,
    )
    adapter.generate(request, run)
    run.end()

    events = reader.get_events(run.run_id)
    assert [e.type for e in events] == ["run_start", "model_request", "model_response", "run_end"]
    stored_request = events[1].payload
    assert stored_request["text_parts"] == ["mail [REDACTED:email] [REDACTED:secret]"]
    assert stored_request["images"] == [
        {"sha256": image.sha256, "mime_type": "image/png", "size_bytes": len(image.data)}
    ]
    assert "fake-image-bytes" not in str(stored_request)
    assert events[2].parent_seq == 2
    assert reader.verify_chain(run.run_id).ok

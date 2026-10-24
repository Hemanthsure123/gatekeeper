"""Writing audit events.

Fail closed: if an event cannot be redacted, serialized or written, ``record`` raises
:class:`~gatekeeper.errors.AuditError` and the run refuses every later write. Callers
must not continue the step: a decision that was not recorded must not happen.

Each :class:`Run` has a single writer. It tracks ``seq`` and the previous chain hash in
memory; the unique ``(run_id, seq)`` constraint is the safety net against a second
writer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit.hashing import canonical_json, chain_hash, sha256_hex
from gatekeeper.audit.types import SCHEMA_VERSION, Actor, EventType
from gatekeeper.db.base import utcnow
from gatekeeper.db.models import Event
from gatekeeper.errors import AuditError
from gatekeeper.redaction import Redactor


@dataclass(frozen=True)
class StoredEvent:
    id: int
    run_id: UUID
    seq: int
    parent_seq: int | None
    item_id: UUID | None
    type: str
    actor: str
    payload: dict[str, Any]
    payload_hash: str
    chain_hash: str
    schema_version: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: Event) -> StoredEvent:
        return cls(
            id=row.id,
            run_id=row.run_id,
            seq=row.seq,
            parent_seq=row.parent_seq,
            item_id=row.item_id,
            type=row.type,
            actor=row.actor,
            payload=row.payload,
            payload_hash=row.payload_hash,
            chain_hash=row.chain_hash,
            schema_version=row.schema_version,
            created_at=row.created_at,
        )


class AuditRecorder:
    """Creates runs and writes their events, each in its own committed transaction."""

    def __init__(self, session_factory: sessionmaker[Session], redactor: Redactor) -> None:
        self._session_factory = session_factory
        self._redactor = redactor

    def start_run(
        self, item_id: UUID | None = None, payload: Mapping[str, Any] | None = None
    ) -> Run:
        """Begin a run and record its ``run_start`` event (seq 1)."""
        run = Run(self, run_id=uuid4(), item_id=item_id)
        run.record(EventType.RUN_START, Actor.SYSTEM, payload or {})
        return run

    def _prepare(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        redacted = self._redactor.redact(dict(payload))
        body = canonical_json(redacted)
        stored: dict[str, Any] = json.loads(body)
        return stored, sha256_hex(body)

    def _insert(self, event: Event) -> StoredEvent:
        with self._session_factory.begin() as session:
            session.add(event)
            session.flush()
            return StoredEvent.from_row(event)


class Run:
    """One audited run. Not thread-safe: a run has exactly one writer."""

    def __init__(self, recorder: AuditRecorder, *, run_id: UUID, item_id: UUID | None) -> None:
        self._recorder = recorder
        self.run_id = run_id
        self.item_id = item_id
        self._next_seq = 1
        self._previous_chain: str | None = None
        self._ended = False
        self._failed = False

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    @property
    def ended(self) -> bool:
        return self._ended

    def record(
        self,
        event_type: EventType,
        actor: Actor,
        payload: Mapping[str, Any],
        parent_seq: int | None = None,
    ) -> StoredEvent:
        """Redact, hash and durably store one event. Raises AuditError on any failure."""
        seq = self._next_seq
        context = {"run_id": str(self.run_id), "seq": seq, "type": str(event_type)}
        if self._failed:
            raise AuditError("audit run is in a failed state; start a new run", details=context)
        if self._ended:
            raise AuditError("audit run has already ended", details=context)
        if parent_seq is not None and not 1 <= parent_seq < seq:
            raise AuditError("parent_seq must refer to an earlier event", details=context)

        try:
            stored_payload, digest = self._recorder._prepare(payload)
        except (TypeError, ValueError) as exc:
            self._failed = True
            raise AuditError("audit payload is not JSON-serializable", details=context) from exc

        # Assigned here (not by the database) so the hashed value is exactly the stored one.
        created_at = utcnow()
        try:
            link = chain_hash(
                self._previous_chain,
                run_id=self.run_id,
                seq=seq,
                event_type=event_type.value,
                payload_digest=digest,
                actor=actor.value,
                parent_seq=parent_seq,
                item_id=self.item_id,
                schema_version=SCHEMA_VERSION,
                created_at=created_at,
            )
            event = self._recorder._insert(
                Event(
                    run_id=self.run_id,
                    seq=seq,
                    parent_seq=parent_seq,
                    item_id=self.item_id,
                    type=event_type.value,
                    actor=actor.value,
                    payload=stored_payload,
                    payload_hash=digest,
                    chain_hash=link,
                    schema_version=SCHEMA_VERSION,
                    created_at=created_at,
                )
            )
        except Exception as exc:  # fail closed on *any* write problem
            self._failed = True
            raise AuditError("could not write audit event", details=context) from exc

        self._next_seq = seq + 1
        self._previous_chain = link
        if event_type is EventType.RUN_END:
            self._ended = True
        return event

    def end(
        self, status: str = "completed", payload: Mapping[str, Any] | None = None
    ) -> StoredEvent:
        """Record ``run_end``. No further events can be recorded afterwards."""
        return self.record(EventType.RUN_END, Actor.SYSTEM, {**(payload or {}), "status": status})

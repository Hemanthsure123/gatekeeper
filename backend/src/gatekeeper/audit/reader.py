"""Reading audit events and verifying a run's hash chain."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit.hashing import chain_hash, payload_hash
from gatekeeper.audit.recorder import StoredEvent
from gatekeeper.db.models import Event
from gatekeeper.errors import AuditError


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    first_bad_seq: int | None = None
    reason: str | None = None
    checked: int = 0


def verify_events(run_id: UUID, events: Sequence[StoredEvent]) -> ChainVerification:
    """Check seq continuity, payload hashes and the chain. Events must be ordered by seq."""
    if not events:
        return ChainVerification(ok=False, reason="run has no events")
    previous: str | None = None
    for expected_seq, event in enumerate(events, start=1):
        if event.run_id != run_id:
            return ChainVerification(
                False, event.seq, "event belongs to another run", expected_seq - 1
            )
        if event.seq != expected_seq:
            return ChainVerification(
                False, expected_seq, "sequence gap or duplicate", expected_seq - 1
            )
        if payload_hash(event.payload) != event.payload_hash:
            return ChainVerification(False, event.seq, "payload_hash mismatch", expected_seq - 1)
        link = chain_hash(previous, run_id, event.seq, event.type, event.payload_hash)
        if link != event.chain_hash:
            return ChainVerification(False, event.seq, "chain_hash mismatch", expected_seq - 1)
        previous = link
    return ChainVerification(ok=True, checked=len(events))


class AuditReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_events(self, run_id: UUID) -> list[StoredEvent]:
        try:
            with self._session_factory() as session:
                rows = session.scalars(
                    select(Event).where(Event.run_id == run_id).order_by(Event.seq)
                )
                return [StoredEvent.from_row(row) for row in rows]
        except SQLAlchemyError as exc:
            raise AuditError(
                "could not read audit events", details={"run_id": str(run_id)}
            ) from exc

    def verify_chain(self, run_id: UUID) -> ChainVerification:
        return verify_events(run_id, self.get_events(run_id))

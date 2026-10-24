"""Postgres-backed job queue (ADR 0004).

- ``enqueue`` inserts a job; a unique partial index on ``dedupe_key`` (status queued or
  running) turns a duplicate into a no-op that returns the existing job.
- ``claim`` first re-queues jobs whose lease expired (or marks them dead when out of
  attempts), then takes the oldest due job with ``SELECT ... FOR UPDATE SKIP LOCKED``,
  so concurrent workers never claim the same row.
- A running job holds a lease (``locked_until``) that the worker extends with
  heartbeats; ``complete``/``fail`` only succeed for the worker that holds the lock.
- Retryable failures back off exponentially up to ``max_attempts``, then become
  ``dead``; non-retryable failures become ``failed``. Only an error *code* is stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.db.models import ACTIVE_JOB_STATUSES, Job, JobStatus
from gatekeeper.db.session import session_scope

RETRY_BASE_S: Final = 30.0
RETRY_CAP_S: Final = 3600.0


@dataclass(frozen=True)
class EnqueueResult:
    job_id: UUID
    created: bool


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    max_attempts: int,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> EnqueueResult:
    """Insert a job in the caller's transaction (so it commits with related rows)."""
    values: dict[str, Any] = {
        "kind": kind,
        "payload": payload,
        "max_attempts": max_attempts,
        "dedupe_key": dedupe_key,
    }
    if run_after is not None:
        values["run_after"] = run_after
    statement = insert(Job).values(**values)
    if dedupe_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=Job.status.in_(ACTIVE_JOB_STATUSES),
        )
    job_id = session.execute(statement.returning(Job.id)).scalar_one_or_none()
    if job_id is not None:
        return EnqueueResult(job_id, created=True)
    existing = session.execute(
        select(Job.id).where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_JOB_STATUSES))
    ).scalar_one()
    return EnqueueResult(existing, created=False)


def retry_delay(attempts: int, retry_after: float | None = None) -> float:
    """Seconds before a retry: 30 s, 60 s, 120 s ... capped at one hour, never below a
    server-provided ``retry_after``."""
    delay = min(RETRY_CAP_S, RETRY_BASE_S * 2.0 ** max(0, attempts - 1))
    return max(delay, retry_after or 0.0)


_REAP = text(
    """
    UPDATE jobs SET
        status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
        finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
        last_error_code = 'lease_expired',
        locked_by = NULL,
        locked_until = NULL,
        run_after = now(),
        updated_at = now()
    WHERE status = 'running' AND locked_until < now()
    """
)

_CLAIM = text(
    """
    UPDATE jobs SET
        status = 'running',
        attempts = attempts + 1,
        locked_by = :worker,
        locked_until = now() + make_interval(secs => :lease),
        updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY run_after, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, payload, attempts, max_attempts
    """
)

_HEARTBEAT = text(
    """
    UPDATE jobs SET locked_until = now() + make_interval(secs => :lease), updated_at = now()
    WHERE id = :id AND locked_by = :worker AND status = 'running'
    """
)

_COMPLETE = text(
    """
    UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_by = NULL,
        locked_until = NULL, last_error_code = NULL, updated_at = now()
    WHERE id = :id AND locked_by = :worker AND status = 'running'
    """
)


class JobQueue:
    def __init__(
        self, session_factory: sessionmaker[Session], *, lease_s: int, max_attempts: int
    ) -> None:
        self._sessions = session_factory
        self._lease_s = lease_s
        self._max_attempts = max_attempts

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
    ) -> EnqueueResult:
        with session_scope(self._sessions) as session:
            return enqueue(
                session,
                kind,
                payload,
                max_attempts=self._max_attempts,
                dedupe_key=dedupe_key,
                run_after=run_after,
            )

    def reap_expired(self) -> int:
        with session_scope(self._sessions) as session:
            return session.execute(_REAP).rowcount  # type: ignore[attr-defined, no-any-return]

    def claim(self, worker_id: str) -> ClaimedJob | None:
        self.reap_expired()
        with session_scope(self._sessions) as session:
            row = session.execute(_CLAIM, {"worker": worker_id, "lease": self._lease_s}).first()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            kind=row.kind,
            payload=dict(row.payload),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
        )

    def heartbeat(self, job_id: UUID, worker_id: str) -> bool:
        with session_scope(self._sessions) as session:
            result = session.execute(
                _HEARTBEAT, {"id": job_id, "worker": worker_id, "lease": self._lease_s}
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def complete(self, job_id: UUID, worker_id: str) -> bool:
        """Mark succeeded. False if this worker no longer holds the job (lease lost)."""
        with session_scope(self._sessions) as session:
            result = session.execute(_COMPLETE, {"id": job_id, "worker": worker_id})
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        code: str,
        retryable: bool,
        retry_after: float | None = None,
    ) -> str | None:
        """Record a failure. Returns the new status, or None if the lock was lost."""
        with session_scope(self._sessions) as session:
            job = session.execute(
                select(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running")
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                return None
            job.last_error_code = code
            job.locked_by = None
            job.locked_until = None
            if retryable and job.attempts < job.max_attempts:
                job.status = JobStatus.QUEUED.value
                now = session.execute(select(func.now())).scalar_one()
                job.run_after = now + timedelta(seconds=retry_delay(job.attempts, retry_after))
            else:
                job.status = (JobStatus.DEAD if retryable else JobStatus.FAILED).value
                job.finished_at = session.execute(select(func.now())).scalar_one()
            return job.status

    def recent(self, limit: int = 20) -> Sequence[Job]:
        with session_scope(self._sessions) as session:
            return list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)))

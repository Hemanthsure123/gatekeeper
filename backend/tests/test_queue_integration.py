"""Integration tests for the Postgres job queue, worker and webhook store."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, text
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.api.store import PostgresWebhookStore
from gatekeeper.api.webhooks import DeliveryRecord, DeliveryStatus, PrJob
from gatekeeper.db.models import Job
from gatekeeper.db.session import create_db_engine, create_session_factory, session_scope
from gatekeeper.errors import ExternalServiceError, ValidationFailed
from gatekeeper.queue import JobQueue, retry_delay
from gatekeeper.queue.jobs import ClaimedJob
from gatekeeper.queue.worker import JobContext, Worker

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def queue_engine(
    make_database: Callable[[], URL], make_alembic_config: Callable[[URL], Config]
) -> Iterator[Engine]:
    url = make_database()
    command.upgrade(make_alembic_config(url), "head")
    engine = create_db_engine(url=url, pool_size=10)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(queue_engine: Engine) -> sessionmaker[Session]:
    with queue_engine.begin() as connection:
        connection.execute(text("DELETE FROM jobs"))
        connection.execute(text("DELETE FROM webhook_deliveries"))
    return create_session_factory(queue_engine)


@pytest.fixture
def queue(sessions: sessionmaker[Session]) -> JobQueue:
    return JobQueue(sessions, lease_s=60, max_attempts=2)


def job_row(sessions: sessionmaker[Session], job_id: UUID) -> Job:
    with session_scope(sessions) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def expire_lease(sessions: sessionmaker[Session], job_id: UUID) -> None:
    with session_scope(sessions) as session:
        session.execute(
            text("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


def make_due(sessions: sessionmaker[Session], job_id: UUID) -> None:
    with session_scope(sessions) as session:
        session.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})


def test_concurrent_workers_never_double_claim(queue: JobQueue) -> None:
    ids = {queue.enqueue("k", {"n": n}).job_id for n in range(30)}
    claimed: list[list[UUID]] = [[], []]
    barrier = threading.Barrier(2)

    def drain(slot: int) -> None:
        barrier.wait()
        while (job := queue.claim(f"worker-{slot}")) is not None:
            claimed[slot].append(job.id)

    threads = [threading.Thread(target=drain, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = claimed[0] + claimed[1]
    assert len(everything) == len(set(everything)) == 30
    assert set(everything) == ids


def test_expired_lease_is_reclaimed(queue: JobQueue, sessions: sessionmaker[Session]) -> None:
    job_id = queue.enqueue("k", {}).job_id
    first = queue.claim("a")
    assert first is not None
    assert first.attempts == 1
    assert queue.claim("b") is None  # still leased
    expire_lease(sessions, job_id)
    second = queue.claim("b")
    assert second is not None
    assert (second.id, second.attempts) == (job_id, 2)
    assert queue.complete(job_id, "a") is False  # a lost the lease
    assert queue.heartbeat(job_id, "a") is False
    assert queue.heartbeat(job_id, "b") is True
    assert queue.complete(job_id, "b") is True
    assert job_row(sessions, job_id).status == "succeeded"


def test_expired_lease_without_attempts_left_is_dead(
    queue: JobQueue, sessions: sessionmaker[Session]
) -> None:
    job_id = queue.enqueue("k", {}).job_id
    queue.claim("a")
    expire_lease(sessions, job_id)
    queue.claim("b")
    expire_lease(sessions, job_id)
    assert queue.claim("c") is None
    row = job_row(sessions, job_id)
    assert (row.status, row.last_error_code) == ("dead", "lease_expired")


def test_retry_then_dead(queue: JobQueue, sessions: sessionmaker[Session]) -> None:
    job_id = queue.enqueue("k", {}).job_id
    queue.claim("w")
    assert queue.fail(job_id, "w", code="external_service_error", retryable=True) == "queued"
    row = job_row(sessions, job_id)
    assert row.run_after > row.updated_at  # backed off into the future
    assert queue.claim("w") is None
    make_due(sessions, job_id)
    assert queue.claim("w") is not None
    assert queue.fail(job_id, "w", code="external_service_error", retryable=True) == "dead"
    row = job_row(sessions, job_id)
    assert (row.status, row.attempts, row.last_error_code) == ("dead", 2, "external_service_error")
    assert row.finished_at is not None


def test_non_retryable_failure_is_final(queue: JobQueue, sessions: sessionmaker[Session]) -> None:
    job_id = queue.enqueue("k", {}).job_id
    queue.claim("w")
    assert queue.fail(job_id, "w", code="validation_failed", retryable=False) == "failed"
    assert queue.fail(job_id, "w", code="x", retryable=False) is None  # no longer held


def test_dedupe_key_only_blocks_active_jobs(queue: JobQueue) -> None:
    first = queue.enqueue("k", {}, dedupe_key="pr:o/n#1@abc")
    again = queue.enqueue("k", {}, dedupe_key="pr:o/n#1@abc")
    assert first.created is True
    assert (again.job_id, again.created) == (first.job_id, False)
    claimed = queue.claim("w")
    assert claimed is not None
    assert queue.enqueue("k", {}, dedupe_key="pr:o/n#1@abc").created is False  # running
    queue.complete(first.job_id, "w")
    assert queue.enqueue("k", {}, dedupe_key="pr:o/n#1@abc").created is True


def test_retry_delay_backs_off_and_honours_retry_after() -> None:
    assert [retry_delay(n) for n in (1, 2, 3)] == [30.0, 60.0, 120.0]
    assert retry_delay(20) == 3600.0
    assert retry_delay(1, retry_after=500) == 500


def test_worker_processes_jobs_and_applies_retry_rules(
    queue: JobQueue, sessions: sessionmaker[Session]
) -> None:
    seen: list[ClaimedJob] = []

    def ok(job: ClaimedJob, ctx: JobContext) -> None:
        ctx.check_deadline()
        seen.append(job)

    def flaky(_job: ClaimedJob, _ctx: JobContext) -> None:
        raise ExternalServiceError("GitHub down", details={"retry_after": 90})

    def broken(_job: ClaimedJob, _ctx: JobContext) -> None:
        raise ValidationFailed("bad payload")

    def crash(_job: ClaimedJob, _ctx: JobContext) -> None:
        raise RuntimeError("bug with a secret token in the message")

    worker = Worker(
        queue,
        {"ok": ok, "flaky": flaky, "broken": broken, "crash": crash},
        poll_interval_s=0.01,
        job_timeout_s=60,
        lease_s=60,
        worker_id="w",
    )
    ids = {
        kind: queue.enqueue(kind, {"x": 1}).job_id
        for kind in ("ok", "flaky", "broken", "crash", "nope")
    }
    while worker.run_once():
        pass
    status = {kind: job_row(sessions, job_id) for kind, job_id in ids.items()}
    assert status["ok"].status == "succeeded"
    assert seen[0].payload == {"x": 1}
    assert (status["flaky"].status, status["flaky"].last_error_code) == (
        "queued",
        "external_service_error",
    )
    assert (status["broken"].status, status["broken"].last_error_code) == (
        "failed",
        "validation_failed",
    )
    assert (status["crash"].status, status["crash"].last_error_code) == ("failed", "internal_error")
    assert (status["nope"].status, status["nope"].last_error_code) == ("failed", "unknown_job_kind")


def test_worker_timeout_marks_job_for_retry(
    queue: JobQueue, sessions: sessionmaker[Session]
) -> None:
    def slow(_job: ClaimedJob, ctx: JobContext) -> None:
        ctx.check_deadline()

    worker = Worker(queue, {"slow": slow}, poll_interval_s=0.01, job_timeout_s=-1, lease_s=60)
    job_id = queue.enqueue("slow", {}).job_id
    assert worker.run_once()
    assert job_row(sessions, job_id).last_error_code == "job_timeout"


def test_worker_loop_stops_when_requested(queue: JobQueue) -> None:
    worker = Worker(queue, {}, poll_interval_s=0.01, job_timeout_s=60, lease_s=60)
    thread = threading.Thread(target=worker.run)
    thread.start()
    worker.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_postgres_webhook_store(sessions: sessionmaker[Session], queue: JobQueue) -> None:
    store = PostgresWebhookStore(sessions, max_attempts=3)
    record = DeliveryRecord("del-1", "pull_request", "opened", "octo/play")
    assert store.register(record) is None
    assert store.register(record) is DeliveryStatus.RECEIVED
    job = PrJob("octo", "play", 5, "e" * 40, "del-1")
    store.enqueue_pr(job)
    store.enqueue_pr(job)  # deduped, still one job
    assert store.register(record) is DeliveryStatus.QUEUED
    jobs = queue.recent()
    assert len(jobs) == 1
    assert (jobs[0].kind, jobs[0].payload["number"], jobs[0].max_attempts) == ("process_pr", 5, 3)
    store.mark("del-1", DeliveryStatus.IGNORED)
    assert store.register(record) is DeliveryStatus.IGNORED

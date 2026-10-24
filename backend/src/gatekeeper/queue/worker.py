"""Single-process worker loop for the Postgres queue.

- One job at a time by default: local model inference is CPU-bound.
- Polls every WORKER_POLL_INTERVAL_S with jitter; an idle wait wakes immediately on
  shutdown.
- A heartbeat thread extends the job's lease every ``lease / 3`` seconds until the job
  finishes or JOB_TIMEOUT_S passes. A job past its timeout loses its lease and is
  reclaimed; handlers should call ``ctx.check_deadline()`` between steps (Python cannot
  pre-empt a running call; model calls have their own MODEL_TIMEOUT_S).
- Ctrl+C (and Ctrl+Break on Windows) asks for a graceful stop: the current job
  finishes, then the loop exits. A second Ctrl+C aborts immediately; the job's lease
  then expires and another worker reclaims it.
"""

from __future__ import annotations

import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType
from typing import Final
from uuid import uuid4

import httpx
from sqlalchemy.exc import OperationalError

from gatekeeper.errors import GatekeeperError, JobTimeout
from gatekeeper.logging import get_logger, log_context
from gatekeeper.queue.jobs import ClaimedJob, JobQueue

log = get_logger("gatekeeper.queue.worker")

UNKNOWN_KIND: Final = "unknown_job_kind"


@dataclass(frozen=True)
class JobContext:
    job: ClaimedJob
    deadline: float  # time.monotonic() value

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise JobTimeout("job exceeded JOB_TIMEOUT_S")


Handler = Callable[[ClaimedJob, JobContext], None]


def classify(exc: BaseException) -> tuple[str, bool, float | None]:
    """(error code, retryable, retry_after) for a handler failure. Never a stack trace."""
    if isinstance(exc, GatekeeperError):
        retry_after = exc.details.get("retry_after")
        return exc.code, exc.retryable, float(retry_after) if retry_after else None
    if isinstance(exc, OperationalError | httpx.TransportError):
        return "transient_error", True, None
    return "internal_error", False, None


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        poll_interval_s: float,
        job_timeout_s: float,
        lease_s: int,
        worker_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.handlers = dict(handlers)
        self.poll_interval_s = poll_interval_s
        self.job_timeout_s = job_timeout_s
        self.lease_s = lease_s
        self.worker_id = worker_id or default_worker_id()
        self.stop_event = threading.Event()

    # -- loop ---------------------------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        log.info("worker started", worker_id=self.worker_id)
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except OperationalError as exc:
                log.warning("database unavailable; retrying", error=type(exc).__name__)
                worked = False
            if not worked:
                self.stop_event.wait(self.poll_interval_s * (0.75 + random.random() / 2))  # noqa: S311
        log.info("worker stopped", worker_id=self.worker_id)

    def run_once(self) -> bool:
        """Claim and process one job. False when the queue had nothing due."""
        job = self.queue.claim(self.worker_id)
        if job is None:
            return False
        self.process(job)
        return True

    def process(self, job: ClaimedJob) -> None:
        context = JobContext(job=job, deadline=time.monotonic() + self.job_timeout_s)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(job, context, stop_heartbeat), daemon=True
        )
        with log_context(job_id=job.id):
            log.info("job started", kind=job.kind, attempt=job.attempts)
            heartbeat.start()
            try:
                handler = self.handlers.get(job.kind)
                if handler is None:
                    status = self.queue.fail(
                        job.id, self.worker_id, code=UNKNOWN_KIND, retryable=False
                    )
                    log.error("unknown job kind", kind=job.kind, status=status)
                    return
                handler(job, context)
                context.check_deadline()
            except Exception as exc:
                code, retryable, retry_after = classify(exc)
                status = self.queue.fail(
                    job.id, self.worker_id, code=code, retryable=retryable, retry_after=retry_after
                )
                log.warning("job failed", code=code, retryable=retryable, status=status)
                return
            finally:
                stop_heartbeat.set()
                heartbeat.join()
            if self.queue.complete(job.id, self.worker_id):
                log.info("job succeeded")
            else:
                log.warning("job finished after losing its lease; result kept, status unchanged")

    def _heartbeat(self, job: ClaimedJob, context: JobContext, stop: threading.Event) -> None:
        interval = max(1.0, self.lease_s / 3)
        while not stop.wait(interval):
            if time.monotonic() > context.deadline:
                log.warning("job exceeded JOB_TIMEOUT_S; no longer extending its lease")
                return
            try:
                if not self.queue.heartbeat(job.id, self.worker_id):
                    log.warning("lost the job lease")
                    return
            except Exception as exc:  # keep trying; the lease has slack
                log.warning("heartbeat failed", error=type(exc).__name__)

    # -- shutdown -----------------------------------------------------------------------

    def request_stop(self) -> None:
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handle(signum: int, _frame: FrameType | None) -> None:
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            log.info("stop requested; finishing the current job (press Ctrl+C again to abort)")
            self.stop_event.set()

        signal.signal(signal.SIGINT, handle)
        sigbreak = getattr(signal, "SIGBREAK", None)  # Windows Ctrl+Break
        if sigbreak is not None:
            signal.signal(sigbreak, handle)

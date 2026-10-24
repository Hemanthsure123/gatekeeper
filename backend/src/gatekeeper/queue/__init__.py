"""Postgres-backed job queue and worker (ADR 0004). No Redis, RQ or Celery."""

from typing import Final

from gatekeeper.queue.jobs import ClaimedJob, EnqueueResult, JobQueue, enqueue, retry_delay

#: Job kinds.
PROCESS_PR: Final = "process_pr"

__all__ = ["PROCESS_PR", "ClaimedJob", "EnqueueResult", "JobQueue", "enqueue", "retry_delay"]

"""``python -m gatekeeper.worker`` (``make worker``): run one queue worker."""

from __future__ import annotations

import sys

from gatekeeper.config import get_settings
from gatekeeper.errors import GatekeeperError
from gatekeeper.logging import configure_logging
from gatekeeper.pipeline.process_pr import Services, process_pr_handler
from gatekeeper.queue import PROCESS_PR, JobQueue
from gatekeeper.queue.worker import Worker


def main() -> int:
    try:
        settings = get_settings()
        settings.require("DATABASE_URL")
        configure_logging(settings)
        services = Services.from_settings(settings)
    except GatekeeperError as exc:
        print(f"Cannot start the worker: {exc.message}", file=sys.stderr)
        return 2
    queue = JobQueue(
        services.session_factory,
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    worker = Worker(
        queue,
        {PROCESS_PR: process_pr_handler(services)},
        poll_interval_s=settings.worker_poll_interval_s,
        job_timeout_s=settings.job_timeout_s,
        lease_s=settings.job_lease_s,
    )
    try:
        worker.run()
    except KeyboardInterrupt:
        print("Worker aborted; an unfinished job will be reclaimed after its lease expires.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

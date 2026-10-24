"""``gatekeeper`` command line (argparse; installed as a console script).

    gatekeeper fetch-pr <https-url | owner/name#number>   process inline, no queue
    gatekeeper enqueue-pr <https-url | owner/name#number> put a process_pr job on the queue
    gatekeeper audit <run_id>                             print events and verify the chain
    gatekeeper jobs [--limit N]                           list recent jobs

Every command respects GATEKEEPER_ALLOWED_REPOS and never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from gatekeeper.api.webhooks import PrJob
from gatekeeper.audit import AuditReader
from gatekeeper.config import Settings, get_settings
from gatekeeper.db.session import create_db_engine, create_session_factory
from gatekeeper.errors import GatekeeperError
from gatekeeper.logging import configure_logging
from gatekeeper.pipeline.process_pr import PrRef, Services, process_pr
from gatekeeper.queue import PROCESS_PR, JobQueue

PAYLOAD_PREVIEW_CHARS = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gatekeeper", description="Gatekeeper command line.")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-pr", help="fetch and process a pull request inline")
    fetch.add_argument("pr", help="https://github.com/owner/name/pull/N or owner/name#N")
    enqueue = commands.add_parser("enqueue-pr", help="queue a pull request for the worker")
    enqueue.add_argument("pr", help="https://github.com/owner/name/pull/N or owner/name#N")
    audit = commands.add_parser("audit", help="print a run's audit events and verify its chain")
    audit.add_argument("run_id", type=UUID, help="the run id (UUID)")
    jobs = commands.add_parser("jobs", help="list recent jobs")
    jobs.add_argument("--limit", type=int, default=20, help="how many jobs (default 20)")
    return parser


def _allowed(settings: Settings, ref: PrRef) -> bool:
    if settings.repo_allowed(ref.owner, ref.name):
        return True
    print(f"Refusing {ref}: the repository is not in GATEKEEPER_ALLOWED_REPOS.", file=sys.stderr)
    return False


def _queue(settings: Settings) -> JobQueue:
    factory = create_session_factory(create_db_engine(settings))
    return JobQueue(factory, lease_s=settings.job_lease_s, max_attempts=settings.job_max_attempts)


def cmd_fetch_pr(settings: Settings, text: str) -> int:
    ref = PrRef.parse(text)
    if not _allowed(settings, ref):
        return 1
    result = process_pr(Services.from_settings(settings), ref, trigger="cli")
    print(f"analysis_id: {result.analysis_id}")
    print(f"run_id:      {result.run_id}")
    print(f"status:      {result.status}")
    return 0


def cmd_enqueue_pr(settings: Settings, text: str) -> int:
    ref = PrRef.parse(text)
    if not _allowed(settings, ref):
        return 1
    job = PrJob(ref.owner, ref.name, ref.number, head_sha=None, delivery_id=None)
    result = _queue(settings).enqueue(PROCESS_PR, job.payload(), dedupe_key=job.dedupe_key())
    state = "queued" if result.created else "already queued (deduplicated)"
    print(f"job {result.job_id}: {state}")
    return 0


def cmd_audit(settings: Settings, run_id: UUID) -> int:
    reader = AuditReader(create_session_factory(create_db_engine(settings)))
    events = reader.get_events(run_id)
    if not events:
        print(f"No audit events for run {run_id}.")
        return 1
    for event in events:
        payload = json.dumps(event.payload, sort_keys=True)
        if len(payload) > PAYLOAD_PREVIEW_CHARS:
            payload = payload[:PAYLOAD_PREVIEW_CHARS] + "..."
        parent = f" parent={event.parent_seq}" if event.parent_seq else ""
        print(
            f"{event.seq:>3} {event.created_at.isoformat()} {event.type:<16} "
            f"{event.actor:<10}{parent} {payload}"
        )
    result = reader.verify_chain(run_id)
    if result.ok:
        print(f"chain: OK ({result.checked} events)")
        return 0
    print(f"chain: BROKEN at seq {result.first_bad_seq} ({result.reason})")
    return 1


def cmd_jobs(settings: Settings, limit: int) -> int:
    rows = _queue(settings).recent(limit)
    if not rows:
        print("No jobs.")
        return 0
    for job in rows:
        print(
            f"{job.id} {job.kind:<12} {job.status:<9} attempts {job.attempts}/{job.max_attempts} "
            f"{job.last_error_code or '-':<24} {job.created_at.isoformat()}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
        configure_logging(settings)
        if args.command == "fetch-pr":
            return cmd_fetch_pr(settings, args.pr)
        if args.command == "enqueue-pr":
            return cmd_enqueue_pr(settings, args.pr)
        if args.command == "audit":
            return cmd_audit(settings, args.run_id)
        return cmd_jobs(settings, args.limit)
    except GatekeeperError as exc:
        print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        return 2 if exc.code in {"config_error", "validation_failed"} else 1


if __name__ == "__main__":
    sys.exit(main())

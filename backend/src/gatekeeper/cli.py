"""``gatekeeper`` command line (argparse; installed as a console script).

    gatekeeper fetch-pr <https-url | owner/name#number> [--json]  review inline, no queue
    gatekeeper show <analysis_id> [--json]                print a stored analysis
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
from typing import Any
from uuid import UUID

from gatekeeper.api.webhooks import PrJob
from gatekeeper.audit import AuditReader
from gatekeeper.config import Settings, get_settings
from gatekeeper.db.models import Analysis
from gatekeeper.db.session import create_db_engine, create_session_factory, session_scope
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
    fetch.add_argument("--json", action="store_true", help="print the stored result as JSON")
    show = commands.add_parser("show", help="print a stored analysis")
    show.add_argument("analysis_id", type=UUID, help="the analysis id (UUID)")
    show.add_argument("--json", action="store_true", help="print the stored result as JSON")
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


def load_analysis(settings: Settings, analysis_id: UUID) -> dict[str, Any] | None:
    factory = create_session_factory(create_db_engine(settings))
    with session_scope(factory) as session:
        row = session.get(Analysis, analysis_id)
        if row is None:
            return None
        return {
            "analysis_id": str(row.id),
            "run_id": str(row.run_id),
            "status": row.status,
            "verdict": row.verdict,
            "confidence": row.confidence,
            "head_sha": row.head_sha,
            "model": row.model,
            "error_code": row.error_code,
            "result": row.result or {},
        }


def format_analysis(summary: dict[str, Any]) -> str:
    result: dict[str, Any] = summary.get("result") or {}
    confidence = summary.get("confidence")
    lines = [
        f"Verdict:    {summary.get('verdict') or '-'} ({summary['status']})",
        f"Confidence: {confidence:.2f}" if confidence is not None else "Confidence: -",
    ]
    if result.get("abstain_reason"):
        lines.append(f"Abstained:  {result['abstain_reason']}")
    if result.get("downgrade_reason"):
        lines.append(f"Downgraded: {result.get('model_verdict')} -> {result['downgrade_reason']}")
    if summary.get("error_code"):
        lines.append(f"Error:      {summary['error_code']}")
    evidence = result.get("evidence") or []
    dropped = result.get("dropped_evidence_count", 0)
    lines.append(f"Evidence ({len(evidence)} verified, {dropped} dropped):")
    for item in evidence:
        span = (
            f"L{item['line_start']}"
            if item["line_start"] == item["line_end"]
            else f"L{item['line_start']}-L{item['line_end']}"
        )
        lines.append(f"  - {item['path']}:{span} [{item['polarity']}] {item['claim']}")
    lines.append("Signals:")
    for signal in result.get("signals") or []:
        truncated = signal["id"] == "truncated_input" and signal["value"]
        if signal["direction"] != "neutral" or truncated:
            lines.append(f"  - {signal['id']} ({signal['direction']}): {signal['explanation']}")
    latency = result.get("latency_ms")
    lines.append(
        f"Model:      {summary.get('model') or '-'}"
        + (f", {latency / 1000:.1f} s" if latency else "")
        + (" (cache hit)" if result.get("cache_hit") else "")
    )
    lines.append(f"Run:        {summary['run_id']}  (gatekeeper audit {summary['run_id']})")
    lines.append(f"Analysis:   {summary['analysis_id']}")
    if result.get("reply_draft"):
        lines.append("\nReply draft (not posted):\n" + result["reply_draft"])
    return "\n".join(lines)


def _print_analysis(settings: Settings, analysis_id: UUID, as_json: bool) -> int:
    summary = load_analysis(settings, analysis_id)
    if summary is None:
        print(f"No analysis {analysis_id}.", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True) if as_json else format_analysis(summary))
    return 0


def cmd_fetch_pr(settings: Settings, text: str, as_json: bool = False) -> int:
    ref = PrRef.parse(text)
    if not _allowed(settings, ref):
        return 1
    result = process_pr(Services.from_settings(settings), ref, trigger="cli")
    if result.skipped and not as_json:
        print(f"Content unchanged since analysis {result.analysis_id}; showing it.\n")
    return _print_analysis(settings, result.analysis_id, as_json)


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
            return cmd_fetch_pr(settings, args.pr, args.json)
        if args.command == "show":
            return _print_analysis(settings, args.analysis_id, args.json)
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

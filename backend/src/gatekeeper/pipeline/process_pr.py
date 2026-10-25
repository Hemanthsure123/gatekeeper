"""The ``process_pr`` job: fetch -> normalise -> store -> analysis + audit run -> steps.

Each run creates a new ``analyses`` row; earlier analyses are never changed. The item's
content is updated to the latest snapshot, and the intake event records which head
commit and content hash this run saw.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import Actor, AuditRecorder, EventType, Redactor
from gatekeeper.config import Settings
from gatekeeper.db.base import utcnow
from gatekeeper.db.models import Analysis, AnalysisStatus, Item, ItemKind
from gatekeeper.db.repos import get_or_create_repo
from gatekeeper.db.session import create_db_engine, create_session_factory, session_scope
from gatekeeper.errors import GatekeeperError, PolicyViolation, ValidationFailed
from gatekeeper.github.client import GitHubClient, GitHubReader
from gatekeeper.github.normalize import content_hash, normalize_pull
from gatekeeper.identity import save_author
from gatekeeper.identity.scrub import forbidden_terms, scrub_content
from gatekeeper.logging import get_logger, log_context
from gatekeeper.pipeline.core import ItemView, Pipeline, PipelineContext, Step
from gatekeeper.pipeline.steps import STUB_STEPS
from gatekeeper.pipeline.version import PIPELINE_VERSION
from gatekeeper.queue.jobs import ClaimedJob
from gatekeeper.queue.worker import Handler, JobContext

STUB_RESULT: Final = {"stub": True}

_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?(?:[?#].*)?$")
_SHORT = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")

log = get_logger("gatekeeper.pipeline.process_pr")


@dataclass(frozen=True)
class PrRef:
    owner: str
    name: str
    number: int

    @classmethod
    def parse(cls, text: str) -> PrRef:
        """``https://github.com/owner/name/pull/12`` or ``owner/name#12``."""
        value = text.strip()
        match = _URL.match(value) or _SHORT.match(value)
        if not match:
            raise ValidationFailed(
                "expected a pull request URL (https://github.com/owner/name/pull/N) or owner/name#N"
            )
        return cls(match[1], match[2], int(match[3]))

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}#{self.number}"


@dataclass(frozen=True)
class ProcessResult:
    analysis_id: UUID
    run_id: UUID
    status: str
    #: True when the content was unchanged and an existing analysis was reused.
    skipped: bool = False


#: Statuses whose analysis can be reused when nothing changed.
REUSABLE_STATUSES: Final = frozenset(
    {AnalysisStatus.COMPLETED.value, AnalysisStatus.ABSTAINED.value}
)


def reusable_analysis(session: Session, item_id: UUID, digest: str) -> Analysis | None:
    """The item's latest analysis, if it saw the same content with this pipeline version."""
    latest = session.scalars(
        select(Analysis)
        .where(Analysis.item_id == item_id)
        .order_by(Analysis.created_at.desc())
        .limit(1)
    ).one_or_none()
    if (
        latest is not None
        and latest.content_hash == digest
        and latest.pipeline_version == PIPELINE_VERSION
        and latest.status in REUSABLE_STATUSES
    ):
        return latest
    return None


@dataclass
class Services:
    settings: Settings
    session_factory: sessionmaker[Session]
    recorder: AuditRecorder
    github: GitHubReader

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        factory = create_session_factory(create_db_engine(settings))
        return cls(
            settings=settings,
            session_factory=factory,
            recorder=AuditRecorder(factory, Redactor.from_settings(settings)),
            github=GitHubClient(settings),
        )


def _upsert_item(
    session: Session, repo_id: UUID, number: int, content: dict[str, Any], digest: str, state: str
) -> Item:
    item = session.execute(
        select(Item).where(
            Item.repo_id == repo_id, Item.kind == ItemKind.PR.value, Item.external_id == str(number)
        )
    ).scalar_one_or_none()
    if item is None:
        item = Item(repo_id=repo_id, kind=ItemKind.PR.value, external_id=str(number))
        session.add(item)
    item.content = content
    item.content_hash = digest
    item.state = state
    session.flush()
    return item


def _finish_analysis(
    factory: sessionmaker[Session], analysis_id: UUID, status: AnalysisStatus, **values: Any
) -> None:
    with session_scope(factory) as session:
        session.execute(
            update(Analysis)
            .where(Analysis.id == analysis_id)
            .values(status=status.value, finished_at=utcnow(), **values)
        )


def process_pr(
    services: Services,
    ref: PrRef,
    *,
    trigger: str,
    steps: Sequence[Step] = STUB_STEPS,
    check_deadline: Callable[[], None] = lambda: None,
) -> ProcessResult:
    settings = services.settings
    if not settings.repo_allowed(ref.owner, ref.name):
        raise PolicyViolation(
            f"{ref.owner}/{ref.name} is not in GATEKEEPER_ALLOWED_REPOS; refusing to process it"
        )

    github = services.github
    pr = github.get_pull(ref.owner, ref.name, ref.number)
    files = github.list_pull_files(ref.owner, ref.name, ref.number, max_files=settings.pr_max_files)
    checks = github.list_check_runs(ref.owner, ref.name, pr.head.sha)
    try:
        created_at = github.get_user_created_at(ref.owner, ref.name, pr.user.id)
    except GatekeeperError as exc:  # advisory data only: never block the pipeline on it
        log.warning("could not fetch account creation date", code=exc.code)
        created_at = None
    check_deadline()

    raw_content, author = normalize_pull(
        pr, files, checks, diff_max_bytes=settings.diff_max_bytes, account_created_at=created_at
    )
    # The trusted boundary: identity leaves the text here, before storage or review.
    content = scrub_content(raw_content, author)
    terms = forbidden_terms(author)
    digest = content_hash(content)
    state = "merged" if pr.merged_at else pr.state
    run_id = uuid4()

    with session_scope(services.session_factory) as session:
        repo = get_or_create_repo(session, ref.owner, ref.name)
        if repo.github_repo_id is None and pr.base.repo is not None:
            repo.github_repo_id = pr.base.repo.id
        item = _upsert_item(
            session, repo.id, ref.number, content.model_dump(mode="json"), digest, state
        )
        save_author(session, item.id, author)
        previous = reusable_analysis(session, item.id, digest)
        if previous is not None:
            log.info(
                "content unchanged since the last analysis; skipping",
                analysis_id=str(previous.id),
                pipeline_version=PIPELINE_VERSION,
            )
            return ProcessResult(previous.id, previous.run_id, previous.status, skipped=True)
        analysis = Analysis(
            item_id=item.id,
            run_id=run_id,
            pipeline_version=PIPELINE_VERSION,
            status=AnalysisStatus.RUNNING.value,
            started_at=utcnow(),
            head_sha=content.head_sha,
            content_hash=digest,
        )
        session.add(analysis)
        session.flush()
        item_id, analysis_id = item.id, analysis.id

    with log_context(run_id=run_id, item_id=item_id):
        try:
            run = services.recorder.start_run(
                item_id=item_id,
                run_id=run_id,
                payload={
                    "pipeline_version": PIPELINE_VERSION,
                    "analysis_id": str(analysis_id),
                    "trigger": trigger,
                    "kind": ItemKind.PR.value,
                },
            )
        except GatekeeperError as exc:
            _finish_analysis(
                services.session_factory, analysis_id, AnalysisStatus.FAILED, error_code=exc.code
            )
            raise

        ctx = PipelineContext(
            settings=settings,
            session_factory=services.session_factory,
            run=run,
            item=ItemView(item_id, ItemKind.PR.value, str(ref.number), content, digest),
            analysis_id=analysis_id,
            check_deadline=check_deadline,
            forbidden_terms=terms,
        )
        try:
            Pipeline(steps).run(ctx)
            run.end(status="completed", payload={"result": STUB_RESULT})
        except Exception as exc:
            code = exc.code if isinstance(exc, GatekeeperError) else "internal_error"
            try:
                run.record(EventType.ERROR, Actor.SYSTEM, {"code": code})
                run.end(status="failed")
            except GatekeeperError as audit_exc:
                log.error("could not record the failure in the audit trail", code=audit_exc.code)
            _finish_analysis(
                services.session_factory, analysis_id, AnalysisStatus.FAILED, error_code=code
            )
            log.warning("pipeline failed", code=code)
            raise

        _finish_analysis(
            services.session_factory,
            analysis_id,
            AnalysisStatus.COMPLETED,
            result=dict(STUB_RESULT),
        )
        log.info("pipeline completed", analysis_id=str(analysis_id))
    return ProcessResult(analysis_id, run_id, AnalysisStatus.COMPLETED.value)


def process_pr_handler(services: Services) -> Handler:
    """Queue handler for ``process_pr`` jobs: payload {owner, name, number, delivery_id}."""

    def handle(job: ClaimedJob, ctx: JobContext) -> None:
        payload = job.payload
        try:
            ref = PrRef(str(payload["owner"]), str(payload["name"]), int(payload["number"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("process_pr job payload is malformed") from exc
        trigger = f"webhook:{payload['delivery_id']}" if payload.get("delivery_id") else "queue"
        process_pr(services, ref, trigger=trigger, check_deadline=ctx.check_deadline)

    return handle

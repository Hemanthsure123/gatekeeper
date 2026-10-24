"""process_pr against a fake GitHub reader: unit tests, plus integration runs on Postgres."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import AuditReader, AuditRecorder, Redactor
from gatekeeper.config import Settings
from gatekeeper.db.models import Analysis, Item
from gatekeeper.db.session import session_scope
from gatekeeper.errors import PolicyViolation, ValidationFailed
from gatekeeper.github.models import CheckRun, PullFile, PullFiles, PullRequest
from gatekeeper.identity import get_author
from gatekeeper.pipeline.core import PipelineContext
from gatekeeper.pipeline.process_pr import PrRef, Services, process_pr, process_pr_handler
from gatekeeper.pipeline.steps import STUB_STEPS
from gatekeeper.queue.jobs import ClaimedJob
from gatekeeper.queue.worker import JobContext

LOGIN = "synthetic-author"
NO_DB = cast(sessionmaker[Session], None)


@dataclass
class FakeGitHub:
    head_sha: str = "f" * 40
    calls: list[str] = field(default_factory=list)

    def get_pull(self, owner: str, name: str, number: int) -> PullRequest:
        self.calls.append("pull")
        return PullRequest.model_validate(
            {
                "number": number,
                "state": "open",
                "title": "Add feature",
                "body": "Closes #2",
                "user": {"login": LOGIN, "id": 777},
                "author_association": "FIRST_TIMER",
                "base": {"sha": "0" * 40, "repo": {"id": 31337, "full_name": f"{owner}/{name}"}},
                "head": {"sha": self.head_sha},
                "additions": 1,
                "deletions": 0,
                "changed_files": 1,
            }
        )

    def list_pull_files(self, owner: str, name: str, number: int, *, max_files: int) -> PullFiles:
        file = PullFile(filename="a.py", status="added", additions=1, changes=1, patch="+x")
        return PullFiles(files=[file], truncated=False)

    def list_check_runs(self, owner: str, name: str, ref: str) -> list[CheckRun]:
        return [CheckRun(name="ci", status="queued")]

    def get_user_created_at(self, owner: str, name: str, user_id: int) -> datetime | None:
        return datetime(2019, 1, 1, tzinfo=UTC)


def make_services(factory: sessionmaker[Session], github: FakeGitHub | None = None) -> Services:
    settings = Settings(_env_file=None, gatekeeper_allowed_repos="octo/play")  # type: ignore[call-arg]
    return Services(
        settings=settings,
        session_factory=factory,
        recorder=AuditRecorder(factory, Redactor()),
        github=github or FakeGitHub(),
    )


def latest_analysis(factory: sessionmaker[Session]) -> Analysis:
    with session_scope(factory) as session:
        query = select(Analysis).order_by(Analysis.created_at.desc()).limit(1)
        return session.scalars(query).one()


# -- unit ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/octo/play/pull/12",
        "https://github.com/octo/play/pull/12/",
        " octo/play#12 ",
    ],
)
def test_pr_ref_parsing(text: str) -> None:
    assert PrRef.parse(text) == PrRef("octo", "play", 12)
    assert str(PrRef("octo", "play", 12)) == "octo/play#12"


@pytest.mark.parametrize("text", ["octo/play", "http://github.com/octo/play/pull/1", "octo#1", ""])
def test_pr_ref_rejects_garbage(text: str) -> None:
    with pytest.raises(ValidationFailed):
        PrRef.parse(text)


def test_refuses_repos_outside_the_allowlist() -> None:
    fake = FakeGitHub()
    with pytest.raises(PolicyViolation, match="GATEKEEPER_ALLOWED_REPOS"):
        process_pr(make_services(NO_DB, fake), PrRef("someone", "else", 1), trigger="test")
    assert fake.calls == []


def test_handler_rejects_malformed_payload() -> None:
    handler = process_pr_handler(make_services(NO_DB))
    job = ClaimedJob(UUID(int=1), "process_pr", {"owner": "o"}, attempts=1, max_attempts=3)
    with pytest.raises(ValidationFailed):
        handler(job, JobContext(job=job, deadline=1e18))


# -- integration ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_run_leaves_completed_analysis_and_verifiable_chain(
    session_factory: sessionmaker[Session],
) -> None:
    result = process_pr(make_services(session_factory), PrRef("octo", "play", 41), trigger="test")
    assert result.status == "completed"
    with session_scope(session_factory) as session:
        analysis = session.get(Analysis, result.analysis_id)
        assert analysis is not None
        assert (analysis.status, analysis.result) == ("completed", {"stub": True})
        assert analysis.run_id == result.run_id
        assert analysis.finished_at is not None
        item = session.get(Item, analysis.item_id)
        assert item is not None
        assert LOGIN not in str(item.content)
        author = get_author(session, item.id)
        assert author is not None
        assert (author.login, author.is_first_contribution) == (LOGIN, True)

    reader = AuditReader(session_factory)
    events = reader.get_events(result.run_id)
    assert [e.type for e in events] == [
        "run_start",
        "intake",
        "signal_result",
        "verdict",
        "decision",
        "run_end",
    ]
    assert events[3].payload["stub"] is True
    assert events[-1].payload["status"] == "completed"
    assert reader.verify_chain(result.run_id).ok
    assert all(LOGIN not in str(e.payload) for e in events)


@pytest.mark.integration
def test_new_head_creates_new_analysis_without_touching_old(
    session_factory: sessionmaker[Session],
) -> None:
    ref = PrRef("octo", "play", 42)
    first = process_pr(make_services(session_factory), ref, trigger="test")
    newer = make_services(session_factory, FakeGitHub(head_sha="e" * 40))
    second = process_pr(newer, ref, trigger="test")
    assert first.analysis_id != second.analysis_id
    with session_scope(session_factory) as session:
        ids = [first.analysis_id, second.analysis_id]
        rows = session.scalars(select(Analysis).where(Analysis.id.in_(ids))).all()
        assert len({row.item_id for row in rows}) == 1
        assert all(row.status == "completed" for row in rows)
        item = session.get(Item, rows[0].item_id)
        assert item is not None
        assert item.content["head_sha"] == "e" * 40


@dataclass
class ExplodingStep:
    name: str = "boom"

    def run(self, ctx: PipelineContext) -> None:
        raise ValidationFailed("step failed")


@pytest.mark.integration
def test_step_failure_marks_analysis_failed_and_audits_error(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ValidationFailed):
        process_pr(
            make_services(session_factory),
            PrRef("octo", "play", 43),
            trigger="test",
            steps=(STUB_STEPS[0], ExplodingStep()),
        )
    analysis = latest_analysis(session_factory)
    assert (analysis.status, analysis.error_code) == ("failed", "validation_failed")
    reader = AuditReader(session_factory)
    types = [e.type for e in reader.get_events(analysis.run_id)]
    assert types == ["run_start", "intake", "error", "run_end"]
    assert reader.verify_chain(analysis.run_id).ok


@pytest.mark.integration
def test_queue_handler_runs_the_pipeline(session_factory: sessionmaker[Session]) -> None:
    handler = process_pr_handler(make_services(session_factory))
    payload = {"owner": "octo", "name": "play", "number": 44, "delivery_id": "d-9"}
    job = ClaimedJob(UUID(int=2), "process_pr", payload, attempts=1, max_attempts=3)
    handler(job, JobContext(job=job, deadline=1e18))
    analysis = latest_analysis(session_factory)
    assert analysis.status == "completed"
    start = AuditReader(session_factory).get_events(analysis.run_id)[0]
    assert start.payload["trigger"] == "webhook:d-9"

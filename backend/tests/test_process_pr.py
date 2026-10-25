"""process_pr with a fake GitHub reader and the FakeBackend model: unit tests, plus
integration runs on Postgres that check the stored result and the audit chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import AuditReader, AuditRecorder, Redactor
from gatekeeper.config import Settings
from gatekeeper.db.models import Analysis, Item
from gatekeeper.db.session import session_scope
from gatekeeper.errors import PolicyViolation, PrivateRepoHostedModelBlocked, ValidationFailed
from gatekeeper.github.models import CheckRun, PullFile, PullFiles, PullRequest
from gatekeeper.identity import get_author
from gatekeeper.models.backend import BackendCall
from gatekeeper.models.testing import FakeBackend, fake_adapter
from gatekeeper.pipeline.core import PipelineContext
from gatekeeper.pipeline.process_pr import (
    PrRef,
    Services,
    guard_private_repo,
    process_pr,
    process_pr_handler,
)
from gatekeeper.pipeline.steps import REVIEW_STEPS
from gatekeeper.queue.jobs import ClaimedJob
from gatekeeper.queue.worker import JobContext

LOGIN = "synthetic-author"
NO_DB = cast(sessionmaker[Session], None)
PATCH = "@@ -0,0 +1,2 @@\n+def helper():\n+    return 'x'"
BODY = f"Closes #2. Adds a helper for the parser. Thanks @{LOGIN}, signed {LOGIN}."


def review_json(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "verdict": "looks_valuable",
        "value_score": 0.8,
        "claim_match": "matches",
        "rationale": "Adds a small, coherent helper.",
        "evidence": [
            {
                "path": "src/helper.py",
                "line_start": 1,
                "line_end": 2,
                "quote": "def helper(): return 'x'",
                "claim": "Adds the helper described in the title.",
                "polarity": "supports_value",
            }
        ],
        "contributor_note": "Thanks, this is easy to follow.",
        "self_confidence": 0.95,
    }
    values.update(overrides)
    return values


@dataclass
class FakeGitHub:
    head_sha: str = "f" * 40
    patch: str = PATCH
    private: bool = False
    calls: list[str] = field(default_factory=list)

    def get_pull(self, owner: str, name: str, number: int) -> PullRequest:
        self.calls.append("pull")
        return PullRequest.model_validate(
            {
                "number": number,
                "state": "open",
                "title": "Add parser helper",
                "body": BODY,
                "user": {"login": LOGIN, "id": 777},
                "author_association": "FIRST_TIMER",
                "base": {
                    "sha": "0" * 40,
                    "repo": {"id": 31337, "full_name": f"{owner}/{name}", "private": self.private},
                },
                "head": {"sha": self.head_sha},
                "additions": 2,
                "deletions": 0,
                "changed_files": 1,
            }
        )

    def list_pull_files(self, owner: str, name: str, number: int, *, max_files: int) -> PullFiles:
        added = self.patch.count("\n+")
        file = PullFile(
            filename="src/helper.py",
            status="added",
            additions=added,
            changes=added,
            patch=self.patch,
        )
        return PullFiles(files=[file], truncated=False)

    def list_check_runs(self, owner: str, name: str, ref: str) -> list[CheckRun]:
        return [CheckRun(name="ci", status="completed", conclusion="success")]

    def get_user_created_at(self, owner: str, name: str, user_id: int) -> datetime | None:
        return datetime(2019, 1, 1, tzinfo=UTC)


def make_services(
    factory: sessionmaker[Session],
    github: FakeGitHub | None = None,
    responses: list[Any] | None = None,
    **values: Any,
) -> tuple[Services, FakeBackend]:
    options: dict[str, Any] = {"gatekeeper_allowed_repos": "octo/play", **values}
    settings = Settings(_env_file=None, **options)  # type: ignore[call-arg]
    adapter, fake = fake_adapter(responses if responses is not None else [review_json()])
    services = Services(
        settings=settings,
        session_factory=factory,
        recorder=AuditRecorder(factory, Redactor()),
        github=github or FakeGitHub(),
        model=adapter,
    )
    return services, fake


def analysis_for(factory: sessionmaker[Session], analysis_id: UUID) -> Analysis:
    with session_scope(factory) as session:
        analysis = session.get(Analysis, analysis_id)
        assert analysis is not None
        return analysis


def latest_analysis(factory: sessionmaker[Session]) -> Analysis:
    with session_scope(factory) as session:
        query = select(Analysis).order_by(Analysis.created_at.desc()).limit(1)
        return session.scalars(query).one()


def user_text(call: BackendCall) -> str:
    return " ".join(getattr(part, "text", "") for part in call.parts)


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
    services, _ = make_services(NO_DB, fake)
    with pytest.raises(PolicyViolation, match="GATEKEEPER_ALLOWED_REPOS"):
        process_pr(services, PrRef("someone", "else", 1), trigger="test")
    assert fake.calls == []


def test_handler_rejects_malformed_payload() -> None:
    handler = process_pr_handler(make_services(NO_DB)[0])
    job = ClaimedJob(UUID(int=1), "process_pr", {"owner": "o"}, attempts=1, max_attempts=3)
    with pytest.raises(ValidationFailed):
        handler(job, JobContext(job=job, deadline=1e18))


# -- integration ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_review_stores_result_and_verifiable_chain(
    session_factory: sessionmaker[Session],
) -> None:
    services, fake = make_services(session_factory)
    result = process_pr(services, PrRef("octo", "play", 41), trigger="test")
    analysis = analysis_for(session_factory, result.analysis_id)
    assert (result.status, analysis.status, analysis.verdict) == (
        "completed",
        "completed",
        "looks_valuable",
    )
    stored = analysis.result or {}
    assert analysis.confidence is not None
    assert stored["confidence"] == analysis.confidence >= 0.6
    assert [e["path"] for e in stored["evidence"]] == ["src/helper.py"]
    assert stored["dropped_evidence_count"] == 0
    assert stored["reply_draft"].startswith("**Looks valuable.**")
    assert (stored["pipeline_version"], stored["prompt_version"]) == ("1", "1")
    assert (analysis.model, analysis.prompt_version, analysis.head_sha) == (
        "fake-primary",
        "1",
        "f" * 40,
    )

    (call,) = fake.calls
    assert LOGIN not in user_text(call).lower()
    assert "@[user]" in user_text(call)
    with session_scope(session_factory) as session:
        item = session.get(Item, analysis.item_id)
        assert item is not None
        assert LOGIN not in str(item.content)
        assert item.content_hash == analysis.content_hash
        author = get_author(session, item.id)
        assert author is not None
        assert author.login == LOGIN

    reader = AuditReader(session_factory)
    events = reader.get_events(result.run_id)
    assert [e.type for e in events] == [
        "run_start",
        "intake",
        "signal_result",
        "model_request",
        "model_response",
        "verifier_result",
        "verdict",
        "decision",
        "run_end",
    ]
    assert events[-1].payload["status"] == "completed"
    assert reader.verify_chain(result.run_id).ok
    assert all(LOGIN not in str(e.payload).lower() for e in events)


@pytest.mark.integration
def test_invalid_model_output_abstains(session_factory: sessionmaker[Session]) -> None:
    services, fake = make_services(session_factory, responses=["not json", "still not json"])
    result = process_pr(services, PrRef("octo", "play", 46), trigger="test")
    analysis = analysis_for(session_factory, result.analysis_id)
    stored = analysis.result or {}
    assert (analysis.status, analysis.verdict) == ("abstained", "needs_human_look")
    assert stored["abstain_reason"] == "model_output_invalid"
    assert "A maintainer will take a look" in stored["reply_draft"]
    assert len(fake.calls) == 2


@pytest.mark.integration
def test_too_large_diff_abstains_without_a_model_call(
    session_factory: sessionmaker[Session],
) -> None:
    huge = "@@ -0,0 +1,400 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(400))
    services, fake = make_services(
        session_factory, FakeGitHub(patch=huge), review_diff_token_budget=300
    )
    result = process_pr(services, PrRef("octo", "play", 47), trigger="test")
    analysis = analysis_for(session_factory, result.analysis_id)
    assert analysis.status == "abstained"
    assert (analysis.result or {})["abstain_reason"] == "too_large"
    assert fake.calls == []
    types = [e.type for e in AuditReader(session_factory).get_events(result.run_id)]
    assert "model_request" not in types


@pytest.mark.integration
def test_unproven_negative_verdict_is_downgraded(session_factory: sessionmaker[Session]) -> None:
    negative = review_json(
        verdict="likely_low_value",
        evidence=[
            {
                "path": "src/helper.py",
                "line_start": 1,
                "line_end": 1,
                "quote": "this line does not exist",
                "claim": "Adds nothing useful.",
                "polarity": "raises_concern",
            }
        ],
    )
    services, _ = make_services(session_factory, responses=[negative])
    result = process_pr(services, PrRef("octo", "play", 48), trigger="test")
    stored = analysis_for(session_factory, result.analysis_id).result or {}
    assert stored["model_verdict"] == "likely_low_value"
    assert stored["verdict"] == "needs_human_look"
    assert stored["downgrade_reason"] == "no_verified_concern"
    assert stored["dropped_evidence_count"] == 1


@pytest.mark.integration
def test_new_head_creates_new_analysis_without_touching_old(
    session_factory: sessionmaker[Session],
) -> None:
    ref = PrRef("octo", "play", 42)
    first = process_pr(make_services(session_factory)[0], ref, trigger="test")
    newer = make_services(session_factory, FakeGitHub(head_sha="e" * 40))[0]
    second = process_pr(newer, ref, trigger="test")
    assert first.analysis_id != second.analysis_id
    assert analysis_for(session_factory, first.analysis_id).head_sha == "f" * 40
    assert analysis_for(session_factory, second.analysis_id).head_sha == "e" * 40


@pytest.mark.integration
def test_unchanged_content_reuses_the_latest_analysis(
    session_factory: sessionmaker[Session],
) -> None:
    ref = PrRef("octo", "play", 45)
    first = process_pr(make_services(session_factory)[0], ref, trigger="test")
    services, fake = make_services(session_factory)
    again = process_pr(services, ref, trigger="test")
    assert (again.skipped, again.analysis_id, again.run_id) == (
        True,
        first.analysis_id,
        first.run_id,
    )
    assert fake.calls == []
    changed_head = make_services(session_factory, FakeGitHub(head_sha="d" * 40))[0]
    assert process_pr(changed_head, ref, trigger="test").skipped is False


@dataclass
class ExplodingStep:
    name: str = "boom"

    def run(self, ctx: PipelineContext) -> None:
        raise ValidationFailed("step failed")


@pytest.mark.integration
def test_step_failure_marks_analysis_failed_and_audits_error(
    session_factory: sessionmaker[Session],
) -> None:
    services, _ = make_services(session_factory)
    with pytest.raises(ValidationFailed):
        process_pr(
            services,
            PrRef("octo", "play", 43),
            trigger="test",
            steps=(REVIEW_STEPS[0], ExplodingStep()),
        )
    analysis = latest_analysis(session_factory)
    assert (analysis.status, analysis.error_code) == ("failed", "validation_failed")
    reader = AuditReader(session_factory)
    types = [e.type for e in reader.get_events(analysis.run_id)]
    assert types == ["run_start", "intake", "error", "run_end"]
    assert reader.verify_chain(analysis.run_id).ok


@pytest.mark.integration
def test_queue_handler_runs_the_pipeline(session_factory: sessionmaker[Session]) -> None:
    handler = process_pr_handler(make_services(session_factory)[0])
    payload = {"owner": "octo", "name": "play", "number": 44, "delivery_id": "d-9"}
    job = ClaimedJob(UUID(int=2), "process_pr", payload, attempts=1, max_attempts=3)
    handler(job, JobContext(job=job, deadline=1e18))
    analysis = latest_analysis(session_factory)
    assert analysis.status == "completed"
    start = AuditReader(session_factory).get_events(analysis.run_id)[0]
    assert start.payload["trigger"] == "webhook:d-9"


def private_pull(private: bool | None) -> PullRequest:
    base: dict[str, Any] = {"sha": "0" * 40}
    if private is not None:
        base["repo"] = {"id": 1, "full_name": "octo/play", "private": private}
    return PullRequest.model_validate(
        {
            "number": 1,
            "state": "open",
            "title": "t",
            "user": {"login": "x", "id": 1},
            "base": base,
            "head": {"sha": "f" * 40},
        }
    )


@pytest.mark.parametrize(
    ("backend", "private", "allowed", "blocked"),
    [
        ("gemini_api", True, False, True),
        ("gemini_api", None, False, True),
        ("gemini_api", False, False, False),
        ("gemini_api", True, True, False),
        ("ollama", True, False, False),
    ],
)
def test_private_repo_guard(
    backend: str, private: bool | None, allowed: bool, blocked: bool
) -> None:
    values: dict[str, Any] = {
        "model_backend": backend,
        "allow_private_repos_on_hosted_model": allowed,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    if blocked:
        with pytest.raises(PrivateRepoHostedModelBlocked) as caught:
            guard_private_repo(settings, private_pull(private))
        assert (caught.value.code, caught.value.retryable) == (
            "private_repo_hosted_model_blocked",
            False,
        )
    else:
        guard_private_repo(settings, private_pull(private))


@pytest.mark.integration
def test_private_repo_on_hosted_model_is_a_failed_analysis(
    session_factory: sessionmaker[Session],
) -> None:
    services, fake = make_services(
        session_factory, FakeGitHub(private=True), model_backend="gemini_api"
    )
    with pytest.raises(PrivateRepoHostedModelBlocked):
        process_pr(services, PrRef("octo", "play", 49), trigger="test")
    analysis = latest_analysis(session_factory)
    assert (analysis.status, analysis.error_code) == ("failed", "private_repo_hosted_model_blocked")
    assert fake.calls == []

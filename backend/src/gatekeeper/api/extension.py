"""Extension API, mounted under ``/api``: what the side panel reads, plus the two actions
it may trigger (queueing a review, and posting one approved comment in P06 Part 6).

- **Auth:** every route needs ``Authorization: Bearer <GATEKEEPER_ADMIN_TOKEN>``,
  compared in constant time. Without that setting the API answers 503 and names only
  the missing key. The header is never logged.
- **Identity-free:** responses are built from ``items.content`` (scrubbed at intake),
  ``analyses`` and activity events. Nothing here reads ``item_authors``.
- **Allowlist:** every repository-scoped route respects GATEKEEPER_ALLOWED_REPOS.
- **Errors** use the error model's public dict (see ``gatekeeper.api.app``).
"""

from __future__ import annotations

import hmac
import json
import math
import re
import statistics
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, Header, Query
from fastapi.sse import EventSourceResponse, format_sse_event
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from gatekeeper.activity import (
    TERMINAL_KINDS,
    ActivitySink,
    ActivityView,
    Kind,
    file_states,
    list_events,
)
from gatekeeper.api.webhooks import PrJob
from gatekeeper.audit import Actor, AuditRecorder, EventType, sha256_hex
from gatekeeper.chat import ChatQuestion, answer_question, chat_history
from gatekeeper.config import Settings
from gatekeeper.db.base import utcnow
from gatekeeper.db.models import (
    Analysis,
    AnalysisStatus,
    CommentPost,
    Event,
    Item,
    ItemKind,
    Job,
    Repo,
)
from gatekeeper.db.repos import find_pr_item, latest_analysis
from gatekeeper.db.session import session_scope
from gatekeeper.errors import (
    AuthenticationFailed,
    ConfigError,
    GatekeeperError,
    NotFoundError,
    PolicyViolation,
    ValidationFailed,
)
from gatekeeper.github.client import GitHubClient
from gatekeeper.github.normalize import ItemContent
from gatekeeper.logging import get_logger
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.pipeline.version import PIPELINE_VERSION
from gatekeeper.queue import PROCESS_PR, enqueue
from gatekeeper.review_core.reply import contains_banned, defuse_mentions
from gatekeeper.review_core.signals import SignalInput, SignalResult, file_signals

log = get_logger("gatekeeper.api.extension")

STREAM_POLL_S: Final = 0.5
STREAM_HEARTBEAT_S: Final = 10.0
STREAM_MAX_S: Final = 15 * 60.0
#: Keep streaming this long after run_finished / run_failed, then close.
STREAM_TAIL_S: Final = 1.0
TREE_CACHE_TTL_S: Final = 300.0
RECENT_ANALYSES: Final = 20
FINAL_STATUSES: Final = frozenset(
    {AnalysisStatus.COMPLETED.value, AnalysisStatus.ABSTAINED.value, AnalysisStatus.FAILED.value}
)
REUSABLE: Final = frozenset({AnalysisStatus.COMPLETED.value, AnalysisStatus.ABSTAINED.value})
COMMENT_FOOTER: Final = (
    "_Reviewed by Gatekeeper using an open-weight model. A maintainer approved this comment._"
)
#: First path segments on github.com that are not repository owners.
RESERVED_OWNERS: Final = frozenset(
    {
        "about", "codespaces", "collections", "explore", "features", "issues", "login",
        "marketplace", "new", "notifications", "orgs", "organizations", "pricing", "pulls",
        "search", "settings", "sponsors", "topics", "trending", "enterprise", "apps",
    }
)  # fmt: skip
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


# -- wiring --------------------------------------------------------------------------------


@dataclass
class ExtensionDeps:
    settings: Settings
    session_factory: sessionmaker[Session]
    github: GitHubClient
    recorder: AuditRecorder
    activity: ActivitySink
    #: Builds the model adapter on first use (chat only).
    model_factory: Callable[[], ModelAdapter]
    _model: ModelAdapter | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    tree_cache: dict[tuple[str, str, str], tuple[float, list[tuple[str, str]], bool]] = field(
        default_factory=dict
    )

    def model(self) -> ModelAdapter:
        with self._lock:
            if self._model is None:
                self._model = self.model_factory()
            return self._model


def bearer_guard(settings: Settings) -> Callable[[str | None], None]:
    def guard(authorization: str | None = Header(default=None)) -> None:
        token = settings.gatekeeper_admin_token
        secret = token.get_secret_value() if token is not None else ""
        if not secret:
            raise ConfigError("GATEKEEPER_ADMIN_TOKEN is not set")
        expected = f"Bearer {secret}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise AuthenticationFailed("missing or invalid access token")

    return guard


# -- pure helpers --------------------------------------------------------------------------


def parse_github_url(url: str) -> dict[str, Any]:
    """Parse a github.com page URL into {kind, owner, name, number?, path?, ref?}.

    Kinds: ``pr`` (conversation, commits, checks), ``pr_files`` (Files changed, both the
    classic ``/files`` and the current ``/changes`` route), ``repo``, ``tree``, ``blob``,
    ``other`` (another page of a repository) and ``unsupported``.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in {
        "github.com",
        "www.github.com",
    }:
        return {"kind": "unsupported"}
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return {"kind": "unsupported"}
    owner, name = segments[0], segments[1].removesuffix(".git")
    if owner.lower() in RESERVED_OWNERS or not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name):
        return {"kind": "unsupported"}
    base: dict[str, Any] = {"owner": owner, "name": name}
    rest = segments[2:]
    if not rest:
        return {"kind": "repo", **base}
    if rest[0] == "pull" and len(rest) >= 2 and rest[1].isdigit():
        files = len(rest) >= 3 and rest[2] in {"files", "changes"}
        return {"kind": "pr_files" if files else "pr", **base, "number": int(rest[1])}
    if rest[0] in {"tree", "blob"} and len(rest) >= 2:
        path = "/".join(rest[2:]) or None
        return {"kind": rest[0], **base, "ref": rest[1], "path": path}
    return {"kind": "other", **base}


def ancestors(path: str) -> list[str]:
    """``a/b/c.py`` -> ``["a", "a/b"]``."""
    pieces = path.split("/")[:-1]
    return ["/".join(pieces[: i + 1]) for i in range(len(pieces))]


def build_tree(
    base: Iterable[tuple[str, str]], changed: Mapping[str, str], max_entries: int
) -> tuple[list[dict[str, Any]], bool, int]:
    """Overlay a PR's changed files on the base tree and cap the result.

    Changed paths and their ancestors are always kept; the rest fills up to
    ``max_entries`` in path order. Returns (entries sorted by path, truncated, total).
    """
    entries: dict[str, dict[str, Any]] = {}
    for path, kind in base:
        entries[path] = {"path": path, "type": kind}
    for path, status in changed.items():
        entry = entries.setdefault(path, {"path": path, "type": "blob"})
        entry["status"] = status
        for folder in ancestors(path):
            entries.setdefault(folder, {"path": folder, "type": "tree"})
    total = len(entries)
    keep = set(changed) | {folder for path in changed for folder in ancestors(path)}
    selected = [entries[p] for p in sorted(keep) if p in entries]
    budget = max_entries - len(selected)
    if budget > 0:
        rest = (entries[p] for p in sorted(entries) if p not in keep)
        selected.extend(entry for _, entry in zip(range(budget), rest, strict=False))
    selected.sort(key=lambda e: e["path"])
    return selected, len(selected) < total, total


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


# -- database reads ------------------------------------------------------------------------


def _comment(session: Session, analysis_id: UUID) -> dict[str, Any] | None:
    post = session.get(CommentPost, analysis_id)
    if post is None:
        return None
    return {"html_url": post.html_url, "posted_at": post.posted_at.isoformat()}


def analysis_summary(session: Session, analysis: Analysis) -> dict[str, Any]:
    result: dict[str, Any] = analysis.result or {}
    return {
        "id": str(analysis.id),
        "run_id": str(analysis.run_id),
        "status": analysis.status,
        "verdict": analysis.verdict,
        "confidence": analysis.confidence,
        "claim_match": result.get("claim_match"),
        "abstain_reason": result.get("abstain_reason"),
        "downgrade_reason": result.get("downgrade_reason"),
        "rationale": result.get("rationale"),
        "reply_draft": result.get("reply_draft"),
        "citation_validity": result.get("citation_validity"),
        "dropped_evidence_count": result.get("dropped_evidence_count", 0),
        "model": analysis.model,
        "latency_ms": result.get("latency_ms"),
        "cache_hit": result.get("cache_hit", False),
        "key_alias": result.get("key_alias"),
        "error_code": analysis.error_code,
        "created_at": analysis.created_at.isoformat(),
        "started_at": analysis.started_at.isoformat() if analysis.started_at else None,
        "finished_at": analysis.finished_at.isoformat() if analysis.finished_at else None,
        "comment_posted": _comment(session, analysis.id),
    }


def _fallback_states(content: ItemContent, result: Mapping[str, Any]) -> dict[str, str]:
    """Per-file state for analyses stored before the activity feed existed."""
    cited = {e.get("path") for e in result.get("evidence") or []}
    omitted = set(result.get("omitted_files") or [])
    states = {}
    for file in content.files:
        if file.path in cited:
            states[file.path] = "verified"
        elif file.path in omitted or file.patch is None:
            states[file.path] = "omitted"
        else:
            states[file.path] = "in_context" if result else "listed"
    return states


def _per_file_signals(
    content: ItemContent, result: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    stored = result.get("file_signals")
    if isinstance(stored, dict):
        return {str(k): list(v) for k, v in stored.items()}
    try:
        signals = [SignalResult.model_validate(s) for s in result.get("signals") or []]
        inp = SignalInput(content, omitted_paths=tuple(result.get("omitted_files") or []))
        return {
            path: [s.model_dump(mode="json") for s in fired]
            for path, fired in file_signals(inp, signals).items()
        }
    except ValueError:
        return {}


def pr_view(session: Session, item: Item, analysis: Analysis) -> dict[str, Any]:
    content = ItemContent.model_validate(item.content)
    result: dict[str, Any] = analysis.result or {}
    events = list_events(session, analysis.id)
    states = file_states(events) or _fallback_states(content, result)
    signals = _per_file_signals(content, result)
    evidence: dict[str, list[dict[str, Any]]] = {}
    for entry in result.get("evidence") or []:
        evidence.setdefault(str(entry.get("path")), []).append(
            {
                "line_start": entry.get("line_start"),
                "line_end": entry.get("line_end"),
                "claim": entry.get("claim"),
                "polarity": entry.get("polarity"),
                "quote": entry.get("quote"),
            }
        )
    files = [
        {
            "path": f.path,
            "status": f.status,
            "additions": f.additions,
            "deletions": f.deletions,
            "binary": f.binary,
            "patch_missing": f.patch_missing,
            "state": states.get(f.path, "listed"),
            "signals": signals.get(f.path, []),
            "evidence": evidence.get(f.path, []),
        }
        for f in content.files
    ]
    return {
        "number": int(item.external_id),
        "title": content.title,
        "draft": content.draft,
        "base_sha": content.base_sha,
        "head_sha": content.head_sha,
        "totals": content.totals.model_dump(mode="json"),
        "analysis": analysis_summary(session, analysis),
        "signals": result.get("signals") or [],
        "files": files,
        "activity_last_id": events[-1].id if events else 0,
    }


def model_input(session: Session, analysis: Analysis) -> dict[str, Any] | None:
    event = session.scalars(
        select(Event)
        .where(Event.run_id == analysis.run_id, Event.type == EventType.MODEL_REQUEST.value)
        .order_by(Event.seq)
        .limit(1)
    ).one_or_none()
    if event is None:
        return None
    payload: dict[str, Any] = event.payload
    return {
        "analysis_id": str(analysis.id),
        "model": payload.get("model"),
        "backend": payload.get("backend"),
        "prompt_id": payload.get("prompt_id"),
        "prompt_version": payload.get("prompt_version"),
        "system": payload.get("system", ""),
        "user": "\n\n".join(payload.get("text_parts") or []),
        "images": len(payload.get("images") or []),
        "response_schema_sha256": payload.get("response_schema_sha256"),
        "schema_note": "The response JSON schema (hash above) is appended to the system text.",
        "identity_hidden": True,
    }


def metrics(session: Session, days: int) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(Analysis, Repo.owner, Repo.name, Item.external_id)
        .join(Item, Item.id == Analysis.item_id)
        .join(Repo, Repo.id == Item.repo_id)
        .where(Analysis.created_at >= since)
        .order_by(Analysis.created_at.desc())
    ).all()
    analyses = [row[0] for row in rows]
    results = [a.result or {} for a in analyses]
    confidences = [a.confidence for a in analyses if a.confidence is not None]
    validity = [
        float(r["citation_validity"]) for r in results if r.get("citation_validity") is not None
    ]
    latency = [
        float(r["latency_ms"]) for r in results if r.get("latency_ms") and not r.get("cache_hit")
    ]
    abstained = [
        r.get("abstain_reason") or "unknown"
        for a, r in zip(analyses, results, strict=True)
        if a.status == AnalysisStatus.ABSTAINED.value
    ]
    failed = [
        a.error_code or "unknown" for a in analyses if a.status == AnalysisStatus.FAILED.value
    ]
    jobs = session.execute(
        select(Job.status, func.count()).where(Job.created_at >= since).group_by(Job.status)
    ).all()
    return {
        "days": days,
        "total": len(analyses),
        "by_verdict": dict(Counter(a.verdict for a in analyses if a.verdict)),
        "by_status": dict(Counter(a.status for a in analyses)),
        "abstained": {"count": len(abstained), "reasons": dict(Counter(abstained))},
        "failed": {"count": len(failed), "error_codes": dict(Counter(failed))},
        "confidence": {
            "average": _mean(confidences),
            "median": round(statistics.median(confidences), 4) if confidences else None,
        },
        "citation_validity_average": _mean(validity),
        "latency_ms": {
            "average": round(statistics.fmean(latency), 1) if latency else None,
            "p95": percentile(latency, 0.95),
            "note": "cache hits excluded",
        },
        "recent": [
            {
                "analysis_id": str(a.id),
                "repo": f"{owner}/{name}",
                "number": int(number),
                "verdict": a.verdict,
                "confidence": a.confidence,
                "status": a.status,
                "abstain_reason": (a.result or {}).get("abstain_reason"),
                "latency_ms": (a.result or {}).get("latency_ms"),
                "created_at": a.created_at.isoformat(),
            }
            for a, owner, name, number in rows[:RECENT_ANALYSES]
        ],
        "jobs_by_status": {str(row[0]): int(row[1]) for row in jobs},
        "models": sorted({a.model for a in analyses if a.model}),
    }


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=4_000)


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    number: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=2_000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=50)
    focus_path: str | None = Field(default=None, max_length=500)


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool
    text: str | None = Field(default=None, max_length=65_000)


def comment_text(analysis: Analysis, text: str | None, max_chars: int) -> tuple[str, bool]:
    """The approved text (the stored draft unless edited) and whether it was edited.
    Edited text is length-limited and must pass the banned-word filter."""
    draft = str((analysis.result or {}).get("reply_draft") or "")
    edited = text is not None and text.strip() != draft.strip()
    value = text.strip() if text is not None else draft.strip()
    if not value:
        raise ValidationFailed("the comment text is empty")
    if edited:
        if len(value) > max_chars:
            raise ValidationFailed(f"the comment is longer than {max_chars} characters")
        if contains_banned(value):
            raise ValidationFailed(
                "the text contains wording Gatekeeper never posts because it labels people; "
                "describe the work instead"
            )
    return defuse_mentions(value), edited


# -- router --------------------------------------------------------------------------------


def _check_allowed(settings: Settings, owner: str, name: str) -> None:
    if not settings.repo_allowed(owner, name):
        raise PolicyViolation(f"{owner}/{name} is not enabled (GATEKEEPER_ALLOWED_REPOS)")


def _sse(event: str, data: str, event_id: str | None = None) -> bytes:
    return format_sse_event(data_str=data, event=event, id=event_id)


def build_router(deps: ExtensionDeps) -> APIRouter:
    settings = deps.settings
    sessions = deps.session_factory
    router = APIRouter(dependencies=[Depends(bearer_guard(settings))])

    def load_analysis(session: Session, analysis_id: UUID) -> Analysis:
        analysis = session.get(Analysis, analysis_id)
        if analysis is None:
            raise NotFoundError("no such analysis")
        item = session.get(Item, analysis.item_id)
        repo = session.get(Repo, item.repo_id) if item else None
        if repo is not None:
            _check_allowed(settings, repo.owner, repo.name)
        return analysis

    @router.get("/context")
    def context(url: str = Query(max_length=2_000)) -> dict[str, Any]:
        parsed = parse_github_url(url)
        if parsed["kind"] == "unsupported":
            return {**parsed, "allowed": False, "known": False}
        owner, name = parsed["owner"], parsed["name"]
        allowed = settings.repo_allowed(owner, name)
        known, analysis_id = False, None
        if allowed:
            with session_scope(sessions) as session:
                if "number" in parsed:
                    item = find_pr_item(session, owner, name, parsed["number"])
                    analysis = latest_analysis(session, item.id) if item else None
                    known = analysis is not None
                    analysis_id = str(analysis.id) if analysis else None
                else:
                    known = (
                        session.scalars(
                            select(Repo.id).where(
                                func.lower(Repo.owner) == owner.lower(),
                                func.lower(Repo.name) == name.lower(),
                            )
                        ).first()
                        is not None
                    )
        return {**parsed, "allowed": allowed, "known": known, "analysis_id": analysis_id}

    @router.get("/pr/{owner}/{name}/{number}")
    def pull_request(owner: str, name: str, number: int) -> dict[str, Any]:
        _check_allowed(settings, owner, name)
        with session_scope(sessions) as session:
            item = find_pr_item(session, owner, name, number)
            analysis = latest_analysis(session, item.id) if item else None
            if item is None or analysis is None:
                raise NotFoundError("this pull request has not been analysed yet")
            return {"repo": f"{owner}/{name}", **pr_view(session, item, analysis)}

    @router.post("/pr/{owner}/{name}/{number}/analyze")
    def analyze(owner: str, name: str, number: int, force: bool = False) -> dict[str, Any]:
        _check_allowed(settings, owner, name)
        nonce = uuid4().hex[:12]
        job = PrJob(owner, name, number, head_sha=None, delivery_id=None)
        dedupe = f"pr:{owner}/{name}#{number}@force:{nonce}".lower() if force else job.dedupe_key()
        with session_scope(sessions) as session:
            item = find_pr_item(session, owner, name, number)
            latest = latest_analysis(session, item.id) if item else None
            pending: Analysis | None = None
            if item is not None and (force or latest is None or latest.status not in REUSABLE):
                pending = Analysis(
                    item_id=item.id,
                    run_id=uuid4(),
                    pipeline_version=PIPELINE_VERSION,
                    status=AnalysisStatus.QUEUED.value,
                )
                session.add(pending)
                session.flush()
            payload = {
                **job.payload(),
                "source": "extension",
                "force": force,
                "nonce": nonce if force else None,
                "analysis_id": str(pending.id) if pending else None,
            }
            queued = enqueue(
                session,
                PROCESS_PR,
                payload,
                max_attempts=settings.job_max_attempts,
                dedupe_key=dedupe,
            )
            if not queued.created and pending is not None:
                session.delete(pending)  # an active job already covers this PR
                pending = None
            log.info("review queued from the extension", number=number, force=force)
            return {
                "job_id": str(queued.job_id),
                "created": queued.created,
                "status": "queued" if queued.created else "already_queued",
                "analysis_id": str(pending.id) if pending else None,
            }

    def _events(analysis_id: UUID, after: int) -> tuple[list[ActivityView], str]:
        with session_scope(sessions) as session:
            analysis = load_analysis(session, analysis_id)
            return list_events(session, analysis_id, after=after), analysis.status

    @router.get("/analyses/{analysis_id}/activity")
    def activity(analysis_id: UUID, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
        events, status = _events(analysis_id, after)
        return {
            "analysis_id": str(analysis_id),
            "status": status,
            "events": [e.to_dict() for e in events],
            "finished": any(e.kind in TERMINAL_KINDS for e in events) or status in FINAL_STATUSES,
        }

    @router.get("/analyses/{analysis_id}/stream")
    async def stream(analysis_id: UUID, after: int = Query(default=0, ge=0)) -> EventSourceResponse:
        """Server-Sent Events: history first, then new events (polled every ~500 ms),
        heartbeat comments while idle, closing shortly after run_finished/run_failed."""
        await run_in_threadpool(_events, analysis_id, 0)  # 404/403 before streaming starts

        async def produce() -> AsyncIterator[bytes]:
            last, started = after, time.monotonic()
            last_sent, ended_at = started, None
            while True:
                events, status = await run_in_threadpool(_events, analysis_id, last)
                for event in events:
                    last = event.id
                    last_sent = time.monotonic()
                    yield _sse("activity", _json(event.to_dict()), str(event.id))
                    if event.kind in TERMINAL_KINDS and ended_at is None:
                        ended_at = time.monotonic()
                now = time.monotonic()
                if ended_at is None and not events and status in FINAL_STATUSES:
                    ended_at = now - STREAM_TAIL_S  # finished before the feed existed
                if ended_at is not None and now - ended_at >= STREAM_TAIL_S:
                    yield _sse("end", _json({"status": status}))
                    return
                if now - started > STREAM_MAX_S:
                    yield _sse("timeout", _json({"status": status}))
                    return
                if now - last_sent >= STREAM_HEARTBEAT_S:
                    last_sent = now
                    yield b": heartbeat\n\n"
                await anyio.sleep(STREAM_POLL_S)

        return EventSourceResponse(
            produce(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @router.get("/analyses/{analysis_id}/model-input")
    def analysis_model_input(analysis_id: UUID) -> dict[str, Any]:
        with session_scope(sessions) as session:
            data = model_input(session, load_analysis(session, analysis_id))
        if data is None:
            raise NotFoundError("this run made no model call (for example, the diff was too large)")
        return data

    @router.get("/repos/{owner}/{name}/tree")
    def tree(owner: str, name: str, pr: int = Query(ge=1)) -> dict[str, Any]:
        _check_allowed(settings, owner, name)
        with session_scope(sessions) as session:
            item = find_pr_item(session, owner, name, pr)
            content = ItemContent.model_validate(item.content) if item else None
        if content is not None:
            base_sha, head_sha = content.base_sha, content.head_sha
            changed = {f.path: f.status for f in content.files}
        else:
            pull = deps.github.get_pull(owner, name, pr)
            files = deps.github.list_pull_files(
                owner, name, pr, max_files=settings.pr_max_files
            ).files
            base_sha, head_sha = pull.base.sha, pull.head.sha
            changed = {f.filename: f.status for f in files}
        key = (owner.lower(), name.lower(), base_sha)
        cached = deps.tree_cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            base, gh_truncated = cached[1], cached[2]
        else:
            fetched = deps.github.get_tree(owner, name, base_sha)
            base = [(e.path, e.type) for e in fetched.tree]
            gh_truncated = fetched.truncated
            deps.tree_cache[key] = (time.monotonic() + TREE_CACHE_TTL_S, base, gh_truncated)
        entries, truncated, total = build_tree(base, changed, settings.tree_max_entries)
        return {
            "ref": base_sha,
            "head_sha": head_sha,
            "entries": entries,
            "truncated": truncated or gh_truncated,
            "total": total,
            "changed": len(changed),
        }

    @router.get("/metrics")
    def get_metrics(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
        with session_scope(sessions) as session:
            return metrics(session, days)

    @router.get("/chat/history")
    def get_chat_history(
        owner: str = Query(max_length=100),
        name: str = Query(max_length=100),
        number: int = Query(ge=1),
    ) -> dict[str, Any]:
        _check_allowed(settings, owner, name)
        return {"messages": chat_history(sessions, owner, name, number)}

    @router.post("/chat")
    def chat(body: ChatBody) -> dict[str, Any]:
        """Grounded, read-only Q&A about this PR's review. It cannot trigger actions."""
        _check_allowed(settings, body.owner, body.name)
        reply = answer_question(
            ChatQuestion(
                owner=body.owner,
                name=body.name,
                number=body.number,
                message=body.message,
                history=[(turn.role, turn.content) for turn in body.history],
                focus_path=body.focus_path,
            ),
            settings=settings,
            sessions=sessions,
            recorder=deps.recorder,
            model=deps.model(),
        )
        return {
            "answer": reply.answer,
            "references": reply.references,
            "message_id": str(reply.message_id),
        }

    @router.post("/analyses/{analysis_id}/comment")
    def post_comment(analysis_id: UUID, body: CommentBody) -> dict[str, Any]:
        """The only GitHub write: one comment per analysis, after a maintainer approved
        the exact text. Idempotent via comment_posts; audited before and after posting."""
        if body.confirm is not True:
            raise ValidationFailed("posting needs confirm: true")
        with session_scope(sessions) as session:
            analysis = session.get(Analysis, analysis_id, with_for_update=True)
            if analysis is None:
                raise NotFoundError("no such analysis")
            item = session.get(Item, analysis.item_id)
            repo = session.get(Repo, item.repo_id) if item else None
            if item is None or repo is None or item.kind != ItemKind.PR.value:
                raise NotFoundError("no pull request for this analysis")
            _check_allowed(settings, repo.owner, repo.name)
            existing = session.get(CommentPost, analysis_id)
            if existing is not None:
                return {"html_url": existing.html_url, "already_posted": True}
            if analysis.status not in REUSABLE:
                raise PolicyViolation("only a completed or abstained review can be posted")
            text, edited = comment_text(analysis, body.text, settings.comment_max_chars)
            comment = f"{text}\n\n---\n{COMMENT_FOOTER}"
            digest = sha256_hex(comment.encode("utf-8"))
            owner, name, number = repo.owner, repo.name, int(item.external_id)
            # Fail closed: the approval is recorded before anything is sent to GitHub.
            run = deps.recorder.start_run(
                item_id=item.id,
                payload={"kind": "comment", "analysis_id": str(analysis_id)},
            )
            run.record(
                EventType.ACTION,
                Actor.MAINTAINER,
                {
                    "action": "post_comment",
                    "state": "approved",
                    "analysis_id": str(analysis_id),
                    "body_sha256": digest,
                    "chars": len(comment),
                    "edited": edited,
                },
            )
            try:
                posted = deps.github.create_issue_comment(
                    owner, name, number, comment, allow_write=True
                )
            except GatekeeperError as exc:
                run.record(EventType.ERROR, Actor.GITHUB, {"code": exc.code})
                run.end(status="failed")
                raise
            run.record(
                EventType.ACTION,
                Actor.GITHUB,
                {
                    "action": "post_comment",
                    "state": "posted",
                    "github_comment_id": posted.id,
                    "html_url": posted.html_url,
                },
            )
            run.end(status="completed")
            session.add(
                CommentPost(
                    analysis_id=analysis_id,
                    github_comment_id=posted.id,
                    html_url=posted.html_url,
                    body_hash=digest,
                )
            )
        deps.activity.emit(
            analysis_id,
            Kind.COMMENT_POSTED.value,
            message="A maintainer approved the reply; it was posted to GitHub",
            data={"html_url": posted.html_url, "edited": edited},
        )
        log.info("approved comment posted", number=number, edited=edited)
        return {"html_url": posted.html_url, "already_posted": False}

    return router


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)

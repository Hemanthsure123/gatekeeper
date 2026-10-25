"""Live activity feed: a display-only stream of what a review run is doing right now.

- **The audit trail stays the source of truth.** ``gatekeeper.audit`` records every
  decision, fails closed and is hash-chained for replay. Activity events exist only so
  the extension can show progress (which step, which file) in real time.
- **Best effort.** :meth:`ActivityFeed.emit` never raises: a failure logs a warning and
  the run continues. A missing activity event must never change a review.
- **Identity-free and redacted.** Callers pass step names, paths, counts, codes and
  model claims about the code, never authors. Payloads pass through the Redactor, and a
  payload that contains an identity key (``IDENTITY_KEYS``) is dropped, not stored.
- **Per-file progress** is derived by replaying an analysis' events with
  :func:`file_states` (listed, signalled, in context or omitted, cited, verified or
  dropped). The API reuses that one function.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.db.models import ActivityEvent, find_identity_keys
from gatekeeper.db.session import session_scope
from gatekeeper.logging import get_logger
from gatekeeper.redaction import Redactor

log = get_logger("gatekeeper.activity")

Level = Literal["info", "warn", "error"]
MAX_MESSAGE_CHARS: Final = 500


class Kind(StrEnum):
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    FILES_LISTED = "files_listed"
    FILE_SIGNAL = "file_signal"
    FILE_CONTEXT = "file_context"
    SIGNALS_SUMMARY = "signals_summary"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_FINISHED = "model_call_finished"
    FILE_CITED = "file_cited"
    CITATION_VERIFIED = "citation_verified"
    CITATION_DROPPED = "citation_dropped"
    CONFIDENCE_COMPUTED = "confidence_computed"
    VERDICT = "verdict"
    ABSTAINED = "abstained"
    AWAITING_APPROVAL = "awaiting_approval"
    COMMENT_POSTED = "comment_posted"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


#: A run's feed ends with exactly one of these.
TERMINAL_KINDS: Final = frozenset({Kind.RUN_FINISHED.value, Kind.RUN_FAILED.value})


class ActivitySink(Protocol):
    def emit(
        self,
        analysis_id: UUID,
        kind: str,
        *,
        step: str | None = None,
        path: str | None = None,
        level: Level = "info",
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None: ...


class NullActivity:
    """Discards events (tests, CLI helpers that have no analysis)."""

    def emit(
        self,
        analysis_id: UUID,
        kind: str,
        *,
        step: str | None = None,
        path: str | None = None,
        level: Level = "info",
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        return None


class ActivityFeed:
    """Writes activity events, each in its own short transaction. Never raises."""

    def __init__(self, session_factory: sessionmaker[Session], redactor: Redactor) -> None:
        self._sessions = session_factory
        self._redactor = redactor

    def emit(
        self,
        analysis_id: UUID,
        kind: str,
        *,
        step: str | None = None,
        path: str | None = None,
        level: Level = "info",
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            payload = dict(data or {})
            if find_identity_keys(payload, "data"):
                log.warning("activity event dropped: identity key in payload", kind=kind)
                return
            row = ActivityEvent(
                analysis_id=analysis_id,
                kind=kind,
                step=step,
                path=self._redactor.redact_text(path) if path is not None else None,
                level=level,
                message=self._redactor.redact_text(message)[:MAX_MESSAGE_CHARS],
                data=self._redactor.redact(payload),
            )
            with session_scope(self._sessions) as session:
                session.add(row)
        except Exception as exc:  # best effort by design: never fail the run
            log.warning("activity event dropped", kind=kind, error=type(exc).__name__)


@dataclass(frozen=True)
class ActivityView:
    id: int
    ts: datetime
    kind: str
    step: str | None
    path: str | None
    level: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "step": self.step,
            "path": self.path,
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }


def list_events(
    session: Session, analysis_id: UUID, *, after: int = 0, limit: int = 2_000
) -> list[ActivityView]:
    rows = session.scalars(
        select(ActivityEvent)
        .where(ActivityEvent.analysis_id == analysis_id, ActivityEvent.id > after)
        .order_by(ActivityEvent.id)
        .limit(limit)
    )
    return [
        ActivityView(r.id, r.ts, r.kind, r.step, r.path, r.level, r.message, dict(r.data or {}))
        for r in rows
    ]


# -- per-file progress ---------------------------------------------------------------------

FileState = Literal["listed", "signalled", "in_context", "omitted", "cited", "dropped", "verified"]
#: Progress order. A file keeps its furthest state; one verified citation beats a dropped one.
STATE_RANK: Final[dict[str, int]] = {
    "listed": 0,
    "signalled": 1,
    "in_context": 2,
    "omitted": 2,
    "cited": 3,
    "dropped": 4,
    "verified": 5,
}
_STATE_BY_KIND: Final[dict[str, str]] = {
    Kind.FILE_SIGNAL.value: "signalled",
    Kind.FILE_CITED.value: "cited",
    Kind.CITATION_VERIFIED.value: "verified",
    Kind.CITATION_DROPPED.value: "dropped",
}


def _advance(states: dict[str, str], path: str, state: str) -> None:
    current = states.get(path)
    if current is None or STATE_RANK[state] > STATE_RANK[current]:
        states[path] = state


def file_states(events: Iterable[ActivityView]) -> dict[str, str]:
    """Replay one analysis' events into ``{path: state}``."""
    states: dict[str, str] = {}
    for event in events:
        if event.kind == Kind.FILES_LISTED.value:
            for file in event.data.get("files", []):
                if isinstance(file, Mapping) and isinstance(file.get("path"), str):
                    _advance(states, file["path"], "listed")
            continue
        if event.path is None:
            continue
        if event.kind == Kind.FILE_CONTEXT.value:
            _advance(states, event.path, "in_context" if event.data.get("included") else "omitted")
        elif (state := _STATE_BY_KIND.get(event.kind)) is not None:
            _advance(states, event.path, state)
    return states

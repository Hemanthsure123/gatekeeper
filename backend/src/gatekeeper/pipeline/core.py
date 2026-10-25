"""Pipeline building blocks: the identity-free item view, the step protocol and runner."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import Run
from gatekeeper.config import Settings
from gatekeeper.github.normalize import ItemContent
from gatekeeper.logging import get_logger
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.review_core.diff import DiffView
from gatekeeper.review_core.review import ReviewOutcome, VerdictLabel
from gatekeeper.review_core.signals import SignalResult
from gatekeeper.review_core.verifier import Verification

log = get_logger("gatekeeper.pipeline")


@dataclass(frozen=True)
class ItemView:
    """Everything a step may know about an item. Identity-free by construction: authors
    live in ``gatekeeper.identity`` and are not reachable from here."""

    item_id: UUID
    kind: str
    external_id: str
    content: ItemContent
    content_hash: str


@dataclass
class ReviewState:
    """What the steps hand to each other within one run."""

    diff: DiffView | None = None
    signals: list[SignalResult] = field(default_factory=list)
    review: ReviewOutcome | None = None
    verification: Verification | None = None
    #: Set by a step that decides early that the review must abstain (e.g. too_large).
    abstain_reason: str | None = None
    status: str = "running"
    verdict: VerdictLabel | None = None
    confidence: float | None = None
    result: dict[str, Any] | None = None


def _no_model() -> ModelAdapter:
    raise RuntimeError("no model backend configured for this pipeline")


@dataclass
class PipelineContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    run: Run
    item: ItemView
    analysis_id: UUID
    check_deadline: Callable[[], None] = field(default=lambda: None)
    #: Opaque strings that must never appear in model input (see the leak guard). Steps
    #: do not learn what they are or where they came from.
    forbidden_terms: frozenset[str] = frozenset()
    #: Returns the model adapter; only called when a model call is actually needed.
    get_model: Callable[[], ModelAdapter] = field(default=_no_model)
    state: ReviewState = field(default_factory=ReviewState)


class Step(Protocol):
    name: str

    def run(self, ctx: PipelineContext) -> None: ...


class Pipeline:
    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = tuple(steps)

    def run(self, ctx: PipelineContext) -> None:
        for step in self.steps:
            ctx.check_deadline()
            log.info("pipeline step", step=step.name)
            step.run(ctx)

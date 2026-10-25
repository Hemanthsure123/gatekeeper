"""The single review model call: prompt ``review.pr`` v1 and the ``ReviewResult`` schema.

Exactly one structured call per review (local inference on CPU is slow). The call goes
through the model adapter inside the audit run, so the adapter records the request and
response. Output that stays invalid after the adapter's one repair attempt, or input
that does not fit, ends the analysis as *abstained*, never as a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from gatekeeper.audit import Run
from gatekeeper.config import Settings
from gatekeeper.errors import InputTooLarge, ModelOutputInvalid
from gatekeeper.github.normalize import ItemContent
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.guards import forbidden_terms_guard
from gatekeeper.models.types import ModelRole, Reasoning, Usage
from gatekeeper.prompts import UNTRUSTED_CONTENT_RULE, PromptTemplate
from gatekeeper.review_core.diff import DiffView
from gatekeeper.review_core.signals import SignalResult, signal_summary

VerdictLabel = Literal["looks_valuable", "needs_human_look", "likely_low_value"]
Polarity = Literal["supports_value", "raises_concern"]
ClaimMatch = Literal["matches", "partial", "mismatch", "unclear"]
EVIDENCE_HARD_LIMIT: Final = 20
NO_DIFF: Final = "(no diff is available for this change)"

REVIEW_SYSTEM: Final = f"""You review one GitHub pull request for its maintainers.

Rules:
- Judge only the change and its stated purpose (title and description).
- {UNTRUSTED_CONTENT_RULE}
- Do not guess who wrote the change, and do not guess whether a person or an AI wrote it.
  Do not infer anything about the author. Describe the work, never the person.
- Be neutral and constructive.
- Cite only lines that appear in the diff, using the L<number> new-side line numbers shown.
  Removed lines have no number and cannot be cited. Copy each quote exactly from the
  cited lines.
- Verdict: looks_valuable = a coherent, useful change; needs_human_look = unclear, risky,
  or you are unsure; likely_low_value = little or no useful effect, only with concrete
  evidence that raises a concern.
- value_score and self_confidence are numbers from 0 to 1.
- Answer only with the JSON schema. Keep every field brief."""

REVIEW_PR: Final = PromptTemplate(
    id="review.pr",
    version="1",
    system=REVIEW_SYSTEM,
    user_template=(
        "Review this pull request.\n\n"
        "Title:\n{{title}}\n\n"
        "Description:\n{{body}}\n\n"
        "Signals computed by deterministic code:\n{{signals}}\n\n"
        "Diff:\n{{diff}}"
    ),
    untrusted_slots=frozenset({"title", "body", "signals", "diff"}),
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=200)
    claim: str = Field(min_length=1, max_length=200)
    polarity: Polarity


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: VerdictLabel
    value_score: float = Field(ge=0, le=1)
    claim_match: ClaimMatch
    rationale: str = Field(max_length=600)
    evidence: list[Evidence] = Field(default_factory=list, max_length=EVIDENCE_HARD_LIMIT)
    contributor_note: str = Field(default="", max_length=500)
    self_confidence: float = Field(ge=0, le=1)


@lru_cache(maxsize=8)
def review_result_model(max_evidence: int) -> type[ReviewResult]:
    """ReviewResult with ``evidence`` capped at REVIEW_MAX_EVIDENCE (part of the schema)."""
    model = create_model(
        "ReviewResult",
        __base__=ReviewResult,
        evidence=(list[Evidence], Field(default_factory=list, max_length=max_evidence)),
    )
    return model


@dataclass(frozen=True)
class ReviewOutcome:
    result: ReviewResult | None
    abstain_reason: str | None
    model: str | None
    prompt_id: str
    prompt_version: str
    latency_ms: float | None = None
    usage: Usage | None = None
    cache_hit: bool = False
    #: Backend facts safe to show (for example the Gemini key alias and failovers).
    meta: Mapping[str, Any] = field(default_factory=dict)


def run_review(
    model: ModelAdapter,
    *,
    content: ItemContent,
    diff: DiffView,
    signals: Sequence[SignalResult],
    run: Run,
    forbidden_terms: frozenset[str],
    settings: Settings,
) -> ReviewOutcome:
    rendered = REVIEW_PR.render(
        title=content.title,
        body=content.body,
        signals=signal_summary(signals),
        diff=diff.text or NO_DIFF,
    )
    request = rendered.to_request(
        review_result_model(settings.review_max_evidence),
        role=ModelRole.PRIMARY,
        temperature=0.0,
        max_output_tokens=settings.review_max_output_tokens,
        reasoning=Reasoning.OFF,
    )
    guarded = model.with_validators(forbidden_terms_guard(forbidden_terms))
    try:
        response = guarded.generate(request, run)
    except ModelOutputInvalid:
        return ReviewOutcome(None, "model_output_invalid", None, REVIEW_PR.id, REVIEW_PR.version)
    except InputTooLarge:
        return ReviewOutcome(None, "input_too_large", None, REVIEW_PR.id, REVIEW_PR.version)
    return ReviewOutcome(
        result=response.parsed,
        abstain_reason=None,
        model=response.model,
        prompt_id=REVIEW_PR.id,
        prompt_version=REVIEW_PR.version,
        latency_ms=response.latency_ms,
        usage=response.usage,
        cache_hit=response.cache_hit,
        meta=dict(response.meta),
    )

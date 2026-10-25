"""The review steps: intake, signals, review, verify, decision.

Steps receive only the identity-free ``ItemView`` and an opaque set of forbidden terms.
This module must never import ``gatekeeper.identity`` or ``ItemAuthor`` (enforced by
an AST test). Audit payloads carry hashes, sizes and structured results, never the
author.
"""

from __future__ import annotations

from typing import Any, Final

from gatekeeper.audit import Actor, EventType
from gatekeeper.pipeline.core import PipelineContext, Step
from gatekeeper.pipeline.version import PIPELINE_VERSION
from gatekeeper.review_core.confidence import (
    Decision,
    confidence,
    decide,
    signal_agreement,
)
from gatekeeper.review_core.diff import build_diff_view, text_tokens
from gatekeeper.review_core.reply import compose_reply
from gatekeeper.review_core.review import REVIEW_PR, run_review
from gatekeeper.review_core.signals import SignalInput, compute_signals, is_truncated
from gatekeeper.review_core.verifier import verify


class IntakeStep:
    """Build the citable, budgeted diff view; record hashes and sizes only."""

    name = "intake"

    def run(self, ctx: PipelineContext) -> None:
        content = ctx.item.content
        budget = ctx.settings.review_diff_budget() - text_tokens(content.title + content.body)
        diff = build_diff_view(content, max(0, budget))
        ctx.state.diff = diff
        ctx.run.record(
            EventType.INTAKE,
            Actor.SYSTEM,
            {
                "step": self.name,
                "content_hash": ctx.item.content_hash,
                "head_sha": content.head_sha,
                "files": len(content.files),
                "included_files": len(diff.included),
                "omitted_files": len(diff.omitted),
                "files_without_patch": len(diff.without_patch),
                "diff_estimated_tokens": diff.estimated_tokens,
                "budget_tokens": max(0, budget),
                "too_large": diff.too_large,
                "diff_truncated": content.diff_truncated,
            },
        )


class SignalsStep:
    name = "signals"

    def run(self, ctx: PipelineContext) -> None:
        omitted = ctx.state.diff.omitted if ctx.state.diff else ()
        signals = compute_signals(SignalInput(ctx.item.content, omitted_paths=omitted))
        ctx.state.signals = signals
        ctx.run.record(
            EventType.SIGNAL_RESULT,
            Actor.SYSTEM,
            {"step": self.name, "signals": [s.model_dump(mode="json") for s in signals]},
        )


class ReviewStep:
    """The single model call (skipped, with an abstain, when the diff cannot fit)."""

    name = "review"

    def run(self, ctx: PipelineContext) -> None:
        diff = ctx.state.diff
        if diff is None or diff.too_large:
            ctx.state.abstain_reason = "too_large"
            return
        outcome = run_review(
            ctx.get_model(),
            content=ctx.item.content,
            diff=diff,
            signals=ctx.state.signals,
            run=ctx.run,
            forbidden_terms=ctx.forbidden_terms,
            settings=ctx.settings,
        )
        ctx.state.review = outcome
        if outcome.abstain_reason:
            ctx.state.abstain_reason = outcome.abstain_reason


class VerifyStep:
    name = "verify"

    def run(self, ctx: PipelineContext) -> None:
        review = ctx.state.review
        if review is None or review.result is None or ctx.state.diff is None:
            ctx.run.record(
                EventType.VERIFIER_RESULT,
                Actor.SYSTEM,
                {"step": self.name, "skipped": True, "reason": ctx.state.abstain_reason},
            )
            return
        verification = verify(review.result, ctx.state.diff, ctx.state.signals)
        ctx.state.verification = verification
        ctx.run.record(
            EventType.VERIFIER_RESULT,
            Actor.SYSTEM,
            {
                "step": self.name,
                "kept": len(verification.kept),
                "dropped": [
                    {"index": d.index, "path": d.path, "reason": d.reason}
                    for d in verification.dropped
                ],
                "citation_validity": round(verification.citation_validity, 4),
                "model_verdict": review.result.verdict,
                "verdict": verification.verdict,
                "downgrade_reason": verification.downgrade_reason,
            },
        )


class DecisionStep:
    """Confidence, final verdict, reply draft and the stored result."""

    name = "decision"

    def run(self, ctx: PipelineContext) -> None:
        state = ctx.state
        review, verification = state.review, state.verification
        truncated = is_truncated(state.signals)
        value: float | None = None
        if review is not None and review.result is not None and verification is not None:
            result = review.result
            value = confidence(
                self_confidence=result.self_confidence,
                citation_validity=verification.citation_validity,
                agreement=signal_agreement(verification.verdict, state.signals),
                truncated=truncated,
                looks_valuable_without_evidence=(
                    verification.verdict == "looks_valuable" and not verification.kept
                ),
            )
            decision = decide(
                verification.verdict, value, truncated=truncated, settings=ctx.settings
            )
        else:
            decision = Decision(
                "needs_human_look", "abstained", state.abstain_reason or "no_review"
            )

        abstained = decision.status == "abstained"
        kept = list(verification.kept) if verification else []
        reply = compose_reply(
            verdict=decision.verdict,
            abstained=abstained,
            evidence=kept,
            signals=state.signals,
            contributor_note=review.result.contributor_note if review and review.result else "",
        )
        state.status, state.verdict, state.confidence = decision.status, decision.verdict, value
        state.result = self._result(ctx, decision, value, kept, reply)
        ctx.run.record(
            EventType.VERDICT,
            Actor.SYSTEM,
            {
                "step": self.name,
                "verdict": decision.verdict,
                "confidence": value,
                "status": decision.status,
                "abstain_reason": decision.abstain_reason,
                "model_verdict": review.result.verdict if review and review.result else None,
            },
        )
        ctx.run.record(
            EventType.DECISION,
            Actor.SYSTEM,
            {"step": self.name, "status": decision.status, "action": "none", "posted": False},
        )

    @staticmethod
    def _result(
        ctx: PipelineContext,
        decision: Decision,
        value: float | None,
        kept: list[Any],
        reply: str,
    ) -> dict[str, Any]:
        state = ctx.state
        review = state.review
        result = review.result if review else None
        verification = state.verification
        return {
            "verdict": decision.verdict,
            "status": decision.status,
            "abstain_reason": decision.abstain_reason,
            "confidence": value,
            "value_score": result.value_score if result else None,
            "claim_match": result.claim_match if result else None,
            "model_verdict": result.verdict if result else None,
            "downgrade_reason": verification.downgrade_reason if verification else None,
            "rationale": result.rationale if result else None,
            "evidence": [item.model_dump(mode="json") for item in kept],
            "dropped_evidence_count": len(verification.dropped) if verification else 0,
            "citation_validity": verification.citation_validity if verification else None,
            "signals": [s.model_dump(mode="json") for s in state.signals],
            "omitted_files": list(state.diff.omitted) if state.diff else [],
            "reply_draft": reply,
            "model": review.model if review else None,
            "prompt_id": REVIEW_PR.id,
            "prompt_version": REVIEW_PR.version,
            "pipeline_version": PIPELINE_VERSION,
            "latency_ms": round(review.latency_ms, 1) if review and review.latency_ms else None,
            "cache_hit": review.cache_hit if review else False,
        }


REVIEW_STEPS: Final[tuple[Step, ...]] = (
    IntakeStep(),
    SignalsStep(),
    ReviewStep(),
    VerifyStep(),
    DecisionStep(),
)

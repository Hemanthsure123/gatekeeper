"""The review steps: intake, signals, review, verify, decision.

Steps receive only the identity-free ``ItemView`` and an opaque set of forbidden terms.
This module must never import ``gatekeeper.identity`` or ``ItemAuthor`` (enforced by
an AST test). Audit payloads carry hashes, sizes and structured results, never the
author.
"""

from __future__ import annotations

from typing import Any, Final

from gatekeeper.activity import Kind
from gatekeeper.audit import Actor, EventType
from gatekeeper.github.normalize import FileChange
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
from gatekeeper.review_core.review import REVIEW_PR, Evidence, ReviewOutcome, run_review
from gatekeeper.review_core.signals import (
    SignalInput,
    compute_signals,
    file_signals,
    is_truncated,
)
from gatekeeper.review_core.verifier import verify

VERDICT_TEXT: Final = {
    "looks_valuable": "Looks valuable",
    "needs_human_look": "Needs a human look",
    "likely_low_value": "Likely low value",
}
ABSTAIN_TEXT: Final = {
    "too_large": "the diff is too large to review in one call",
    "input_too_large": "the input does not fit the model's context",
    "model_output_invalid": "the model's answer did not match the schema",
    "truncated_input": "part of the change could not be reviewed",
    "low_confidence": "confidence is below the threshold",
    "negative_needs_more_confidence": "a low-value verdict needs more confidence",
    "no_review": "no review was produced",
}
NO_PATCH_REASON: Final = {
    "diff_size_limit": "over the diff size limit",
    "binary_or_too_large": "binary or too large for GitHub",
}


def _span(item: Evidence) -> str:
    if item.line_start == item.line_end:
        return f"L{item.line_start}"
    return f"L{item.line_start}-L{item.line_end}"


def _citation(item: Evidence) -> dict[str, Any]:
    return {
        "line_start": item.line_start,
        "line_end": item.line_end,
        "polarity": item.polarity,
        "claim": item.claim,
    }


def _file_entry(file: FileChange) -> dict[str, Any]:
    return {
        "path": file.path,
        "status": file.status,
        "additions": file.additions,
        "deletions": file.deletions,
        "binary": file.binary,
        "patch_missing": file.patch_missing,
        "patch_omitted": file.patch_omitted,
    }


class IntakeStep:
    """Build the citable, budgeted diff view; record hashes and sizes only."""

    name = "intake"

    def run(self, ctx: PipelineContext) -> None:
        content = ctx.item.content
        budget = ctx.settings.review_diff_budget() - text_tokens(content.title + content.body)
        diff = build_diff_view(content, max(0, budget))
        ctx.state.diff = diff
        ctx.emit(
            Kind.FILES_LISTED,
            f"Listed {len(content.files)} changed file(s)",
            data={
                "files": [_file_entry(f) for f in content.files],
                "totals": content.totals.model_dump(mode="json"),
                "diff_truncated": content.diff_truncated,
            },
        )
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
        inp = SignalInput(ctx.item.content, omitted_paths=omitted)
        signals = compute_signals(inp)
        ctx.state.signals = signals
        per_file = file_signals(inp, signals)
        ctx.state.file_signals = per_file
        for path, fired in per_file.items():
            names = ", ".join(s.id for s in fired)
            ctx.emit(
                Kind.FILE_SIGNAL,
                f"Checked {path}: " + (f"{names}" if fired else "no file-level rule fired"),
                path=path,
                data={
                    "rules": [
                        {
                            "id": s.id,
                            "direction": s.direction,
                            "severity": s.severity,
                            "explanation": s.explanation,
                        }
                        for s in fired
                    ]
                },
            )
        positive = [s.id for s in signals if s.direction == "positive"]
        negative = [s.id for s in signals if s.direction == "negative"]
        ctx.emit(
            Kind.SIGNALS_SUMMARY,
            f"Signals: {len(positive)} positive, {len(negative)} negative",
            data={
                "positive": positive,
                "negative": negative,
                "neutral": len(signals) - len(positive) - len(negative),
                "truncated": is_truncated(signals),
            },
        )
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
        if diff is not None:
            self._emit_context(ctx)
        if diff is None or diff.too_large:
            ctx.state.abstain_reason = "too_large"
            return
        model = ctx.get_model()
        model_name = model.model_name()
        ctx.emit(
            Kind.MODEL_CALL_STARTED,
            f"Asking {model_name}",
            data={"model": model_name, "backend": model.backend_name},
        )
        outcome = run_review(
            model,
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
        self._emit_finished(ctx, outcome)
        for item in outcome.result.evidence if outcome.result else []:
            ctx.emit(
                Kind.FILE_CITED,
                f"Model cited {item.path}:{_span(item)}",
                path=item.path,
                data=_citation(item),
            )

    @staticmethod
    def _emit_context(ctx: PipelineContext) -> None:
        diff = ctx.state.diff
        if diff is None:
            return
        included, omitted = set(diff.included), set(diff.omitted)
        for file in ctx.item.content.files:
            if file.path in included:
                ctx.emit(
                    Kind.FILE_CONTEXT,
                    f"Reading {file.path}",
                    path=file.path,
                    data={"included": True, "reason": "fits_budget"},
                )
                continue
            if file.path in omitted:
                reason, text = "budget", "the review budget is used up"
            else:
                reason = "diff_size_limit" if file.patch_omitted else "binary_or_too_large"
                text = f"no diff ({NO_PATCH_REASON[reason]})"
            ctx.emit(
                Kind.FILE_CONTEXT,
                f"Left out {file.path}: {text}",
                path=file.path,
                level="warn",
                data={"included": False, "reason": reason},
            )

    @staticmethod
    def _emit_finished(ctx: PipelineContext, outcome: ReviewOutcome) -> None:
        usage = outcome.usage
        seconds = (outcome.latency_ms or 0) / 1000
        if outcome.abstain_reason:
            message = f"The model call ended without a usable answer: {outcome.abstain_reason}"
        elif outcome.cache_hit:
            message = "Answer reused from the cache"
        else:
            message = f"Model answered in {seconds:.1f} s"
            alias, failovers = outcome.meta.get("key_alias"), outcome.meta.get("failovers")
            if alias:
                message += f" ({alias}" + (f", {failovers} failover(s))" if failovers else ")")
        ctx.emit(
            Kind.MODEL_CALL_FINISHED,
            message,
            level="warn" if outcome.abstain_reason else "info",
            data={
                "model": outcome.model,
                "latency_ms": round(outcome.latency_ms, 1) if outcome.latency_ms else None,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cache_hit": outcome.cache_hit,
                "key_alias": outcome.meta.get("key_alias"),
                "failovers": outcome.meta.get("failovers"),
                "evidence": len(outcome.result.evidence) if outcome.result else 0,
                "abstain_reason": outcome.abstain_reason,
            },
        )


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
        dropped = {d.index: d.reason for d in verification.dropped}
        for index, item in enumerate(review.result.evidence):
            if index in dropped:
                ctx.emit(
                    Kind.CITATION_DROPPED,
                    f"Dropped {item.path}:{_span(item)} ({dropped[index]})",
                    path=item.path,
                    level="warn",
                    data={**_citation(item), "reason": dropped[index]},
                )
            else:
                ctx.emit(
                    Kind.CITATION_VERIFIED,
                    f"Verified {item.path}:{_span(item)}",
                    path=item.path,
                    data=_citation(item),
                )
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
            inputs = {
                "self_confidence": result.self_confidence,
                "citation_validity": verification.citation_validity,
                "agreement": signal_agreement(verification.verdict, state.signals),
                "truncated": truncated,
                "looks_valuable_without_evidence": (
                    verification.verdict == "looks_valuable" and not verification.kept
                ),
            }
            value = confidence(**inputs)
            ctx.emit(
                Kind.CONFIDENCE_COMPUTED,
                f"Confidence {value:.2f}",
                data={
                    "confidence": value,
                    "threshold": ctx.settings.review_abstain_threshold,
                    **{k: round(v, 4) if isinstance(v, float) else v for k, v in inputs.items()},
                },
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
        ctx.emit(
            Kind.VERDICT,
            f"Verdict: {VERDICT_TEXT[decision.verdict]}",
            data={
                "verdict": decision.verdict,
                "status": decision.status,
                "confidence": value,
                "model_verdict": review.result.verdict if review and review.result else None,
                "downgrade_reason": verification.downgrade_reason if verification else None,
            },
        )
        if abstained:
            reason = decision.abstain_reason or "no_review"
            ctx.emit(
                Kind.ABSTAINED,
                f"Abstained: {ABSTAIN_TEXT.get(reason, reason)}",
                level="warn",
                data={"reason": reason},
            )
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
            "file_signals": {
                path: [s.model_dump(mode="json") for s in fired]
                for path, fired in state.file_signals.items()
            },
            "omitted_files": list(state.diff.omitted) if state.diff else [],
            "reply_draft": reply,
            "model": review.model if review else None,
            "prompt_id": REVIEW_PR.id,
            "prompt_version": REVIEW_PR.version,
            "pipeline_version": PIPELINE_VERSION,
            "latency_ms": round(review.latency_ms, 1) if review and review.latency_ms else None,
            "cache_hit": review.cache_hit if review else False,
            "key_alias": review.meta.get("key_alias") if review else None,
            "failovers": review.meta.get("failovers") if review else None,
        }


REVIEW_STEPS: Final[tuple[Step, ...]] = (
    IntakeStep(),
    SignalsStep(),
    ReviewStep(),
    VerifyStep(),
    DecisionStep(),
)

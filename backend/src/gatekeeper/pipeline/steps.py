"""Stub steps (P03). Each records its audit event; real logic arrives in later prompts.

Steps receive only the identity-free ``ItemView``. This module must never import
``gatekeeper.identity`` or ``ItemAuthor`` (enforced by an AST test).
"""

from __future__ import annotations

from typing import Final

from gatekeeper.audit import Actor, EventType
from gatekeeper.pipeline.core import PipelineContext, Step


class IntakeStep:
    name = "intake"

    def run(self, ctx: PipelineContext) -> None:
        content = ctx.item.content
        ctx.run.record(
            EventType.INTAKE,
            Actor.SYSTEM,
            {
                "step": self.name,
                "content_hash": ctx.item.content_hash,
                "head_sha": content.head_sha,
                "files": len(content.files),
                "diff_truncated": content.diff_truncated,
            },
        )


class SignalsStep:
    name = "signals"

    def run(self, ctx: PipelineContext) -> None:
        ctx.run.record(
            EventType.SIGNAL_RESULT,
            Actor.SYSTEM,
            {"step": self.name, "stub": True, "content_hash": ctx.item.content_hash},
        )


class ReviewStep:
    """Placeholder for the model review; records a stub verdict, makes no model call."""

    name = "review"

    def run(self, ctx: PipelineContext) -> None:
        ctx.run.record(
            EventType.VERDICT,
            Actor.SYSTEM,
            {"step": self.name, "stub": True, "content_hash": ctx.item.content_hash},
        )


class DecisionStep:
    name = "decision"

    def run(self, ctx: PipelineContext) -> None:
        ctx.run.record(
            EventType.DECISION,
            Actor.SYSTEM,
            {"step": self.name, "stub": True, "action": "none"},
        )


STUB_STEPS: Final[tuple[Step, ...]] = (IntakeStep(), SignalsStep(), ReviewStep(), DecisionStep())

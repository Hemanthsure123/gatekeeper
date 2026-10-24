"""Trivial templates used only by ``make model-check``. Not review prompts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from gatekeeper.prompts.template import UNTRUSTED_CONTENT_RULE, PromptTemplate


class Greeting(BaseModel):
    greeting: str = Field(max_length=80)
    number: int


class ColourAnswer(BaseModel):
    colour: str = Field(max_length=40)


GREETING = PromptTemplate(
    id="model_check.greeting",
    version="1",
    system=f"You are a connectivity check. {UNTRUSTED_CONTENT_RULE}",
    user_template=(
        "Return a short friendly greeting and the number of words in this note.\n{{note}}"
    ),
    untrusted_slots=frozenset({"note"}),
)

VISION = PromptTemplate(
    id="model_check.vision",
    version="1",
    system=f"You are a vision check. {UNTRUSTED_CONTENT_RULE}",
    user_template="Name the single colour that fills the attached image, in one lowercase word.",
)

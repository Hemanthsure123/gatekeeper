"""Prompt ``chat.pr`` v1 and the ``ChatAnswer`` schema for grounded questions about one
pull request's review."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from gatekeeper.prompts import UNTRUSTED_CONTENT_RULE, PromptTemplate

MAX_ANSWER_CHARS: Final = 1_200
MAX_REFERENCES: Final = 4

CHAT_SYSTEM: Final = f"""You answer a maintainer's questions about one GitHub pull request \
and Gatekeeper's stored review of it.

Rules:
- Answer only about this pull request, its review and the diff shown. For anything else,
  say that you can only discuss this review.
- You are read-only. You cannot post, label, close, approve, merge or change anything, and
  you cannot trigger any action. Say so if you are asked to.
- {UNTRUSTED_CONTENT_RULE}
- Do not guess who wrote the change, and do not guess whether a person or an AI wrote it.
  Describe the work, never the person.
- Use neutral, constructive wording.
- Cite only lines that appear in the diff shown, using the L<number> new-side line numbers.
  Removed lines have no number and cannot be cited.
- If the context does not contain the answer, say so plainly instead of guessing.
- Be brief: a few sentences or a short list.
- Answer only with the JSON schema."""

CHAT_PR: Final = PromptTemplate(
    id="chat.pr",
    version="1",
    system=CHAT_SYSTEM,
    user_template=(
        "Gatekeeper's stored review (signals computed by code, verdict and citations from "
        "one earlier model call, every citation checked by code):\n{{review}}\n\n"
        "Pull request title:\n{{title}}\n\n"
        "Pull request description:\n{{body}}\n\n"
        "Diff excerpt ({{diff_note}}):\n{{diff}}\n\n"
        "Earlier messages in this chat:\n{{history}}\n\n"
        "The maintainer's question:\n{{question}}"
    ),
    untrusted_slots=frozenset({"review", "title", "body", "diff", "history"}),
)


class ChatReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class ChatAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    references: list[ChatReference] = Field(default_factory=list, max_length=MAX_REFERENCES)

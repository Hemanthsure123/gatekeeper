"""Prompt templates: versioned, with untrusted content wrapped and escaped."""

from gatekeeper.prompts.template import (
    UNTRUSTED_CONTENT_RULE,
    UNTRUSTED_TAG,
    PromptTemplate,
    RenderedPrompt,
    escape_untrusted,
    wrap_untrusted,
)

__all__ = [
    "UNTRUSTED_CONTENT_RULE",
    "UNTRUSTED_TAG",
    "PromptTemplate",
    "RenderedPrompt",
    "escape_untrusted",
    "wrap_untrusted",
]

"""Small text utilities for the playground."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lower-case ``text`` and join its words with single hyphens."""
    lower_text = text.lower()
    return _NON_ALNUM.sub("-", lower_text).strip("-")


def word_count(text: str) -> int:
    """Number of whitespace-separated words in ``text``."""
    return len(text.split())


def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Shorten ``text`` to at most ``width`` characters, ending with ``suffix``."""
    if len(text) <= width:
        return text
    return text[:width] + suffix

"""Small text utilities."""

import re

_NON_ALNUM  =  re.compile( r"[^a-z0-9]+" )


def slugify( text: str ) -> str:
    """Lower-case ``text`` and join its words with single hyphens."""
    lowered   = text.lower()   
    return _NON_ALNUM.sub("-", lowered).strip("-")


def word_count(text: str) -> int:
    """Number of whitespace-separated words in ``text``."""
    return len( text.split() )



def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Shorten ``text`` to at most ``width`` characters, ending with ``suffix``."""
    if len( text ) <= width:  
        return text
    return text[:width] + suffix

"""Author identity: the ONLY code that reads or writes ``item_authors``.

The review core and pipeline steps must never import this package or the ItemAuthor
model (CLAUDE.md rule a); ``tests/test_contributor_blind_imports.py`` enforces it.
Identity is for audit, advisory display and policy guards only, never for scoring.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from gatekeeper.db.models import ItemAuthor
from gatekeeper.github.normalize import AuthorInfo
from gatekeeper.identity.scrub import forbidden_terms, scrub_text


def save_author(session: Session, item_id: UUID, author: AuthorInfo) -> None:
    """Insert or update the author row for an item."""
    row = session.get(ItemAuthor, item_id)
    if row is None:
        row = ItemAuthor(item_id=item_id, login=author.login, github_user_id=author.github_user_id)
        session.add(row)
    row.login = author.login
    row.github_user_id = author.github_user_id
    row.account_created_at = author.account_created_at
    row.author_association = author.author_association
    row.is_first_contribution = author.is_first_contribution


def forbidden_terms_for_item(session: Session, item_id: UUID) -> frozenset[str]:
    """The leak guard's opaque terms for an item (empty if no author is stored)."""
    author = get_author(session, item_id)
    return forbidden_terms(author) if author is not None else frozenset()


def scrub_for_item(session: Session, item_id: UUID, text: str) -> str:
    """Scrub free text about an item (a chat message) with that item's author."""
    return scrub_text(text, get_author(session, item_id))


def get_author(session: Session, item_id: UUID) -> AuthorInfo | None:
    row = session.get(ItemAuthor, item_id)
    if row is None:
        return None
    return AuthorInfo(
        login=row.login,
        github_user_id=row.github_user_id,
        account_created_at=row.account_created_at,
        author_association=row.author_association,
        is_first_contribution=row.is_first_contribution,
    )

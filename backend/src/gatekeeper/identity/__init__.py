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

"""Repository helpers for the ``repos`` table."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gatekeeper.db.models import Repo


def find_repo(session: Session, owner: str, name: str) -> Repo | None:
    """Case-insensitive lookup by owner/name."""
    return session.scalars(
        select(Repo).where(
            func.lower(Repo.owner) == owner.lower(), func.lower(Repo.name) == name.lower()
        )
    ).one_or_none()


def get_or_create_repo(session: Session, owner: str, name: str) -> Repo:
    """Return the repo matching owner/name in any casing, creating it if absent.

    The casing seen first is kept for display. A concurrent insert of the same repo is
    handled with a savepoint: the loser re-reads the winner's row.
    """
    existing = find_repo(session, owner, name)
    if existing is not None:
        return existing
    repo = Repo(owner=owner, name=name)
    try:
        with session.begin_nested():
            session.add(repo)
    except IntegrityError:
        winner = find_repo(session, owner, name)
        if winner is None:
            raise
        return winner
    return repo

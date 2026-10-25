"""Lookup helpers for repos, pull request items and their analyses."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gatekeeper.db.models import Analysis, Item, ItemKind, Repo


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


def find_pr_item(session: Session, owner: str, name: str, number: int) -> Item | None:
    """The stored pull request item, matching owner/name in any casing."""
    return session.scalars(
        select(Item)
        .join(Repo, Repo.id == Item.repo_id)
        .where(
            func.lower(Repo.owner) == owner.lower(),
            func.lower(Repo.name) == name.lower(),
            Item.kind == ItemKind.PR.value,
            Item.external_id == str(number),
        )
    ).one_or_none()


def latest_analysis(
    session: Session, item_id: UUID, statuses: frozenset[str] | None = None
) -> Analysis | None:
    """The item's newest analysis, optionally only among ``statuses``."""
    query = select(Analysis).where(Analysis.item_id == item_id)
    if statuses is not None:
        query = query.where(Analysis.status.in_(statuses))
    return session.scalars(query.order_by(Analysis.created_at.desc()).limit(1)).one_or_none()

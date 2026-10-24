"""Integration tests for the schema and migrations. Need Postgres (see conftest.py)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import URL, Engine, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.db import (
    Analysis,
    AnalysisStatus,
    Base,
    Item,
    ItemAuthor,
    ItemKind,
    Repo,
    session_scope,
    utcnow,
)

pytestmark = pytest.mark.integration

HASH = "0" * 64


def make_repo(session: Session) -> Repo:
    repo = Repo(owner="octo", name=f"repo-{uuid.uuid4().hex[:8]}")
    session.add(repo)
    session.flush()
    return repo


def test_migration_round_trip_matches_models(
    make_database: Callable[[], URL], make_alembic_config: Callable[[URL], Config]
) -> None:
    url = make_database()
    config = make_alembic_config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert diff == [], f"models and migrations differ: {diff}"
            triggers = connection.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'events'::regclass")
            ).scalars()
            assert set(triggers) == {"events_append_only", "events_no_truncate"}

        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == {"alembic_version"}
            functions = connection.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'events_reject_mutation'")
            ).scalar_one()
            assert functions == 0

        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_core_rows_round_trip(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        repo = make_repo(session)
        item = Item(
            repo_id=repo.id,
            kind=ItemKind.PR,
            external_id="42",
            content={"title": "Fix parser", "diff_stats": {"files": 2}},
            content_hash=HASH,
            state="open",
        )
        session.add(item)
        session.flush()
        session.add(ItemAuthor(item_id=item.id, login="someone", github_user_id=7))
        started = utcnow()
        session.add(
            Analysis(
                item_id=item.id,
                run_id=uuid.uuid4(),
                pipeline_version="p01",
                status=AnalysisStatus.COMPLETED,
                confidence=0.75,
                started_at=started,
                finished_at=started + timedelta(seconds=1),
            )
        )
        item_id = item.id

    with session_scope(session_factory) as session:
        stored = session.get(Item, item_id)
        assert stored is not None
        assert stored.created_at.utcoffset() == timedelta(0)
        assert session.scalars(select(Repo.config).where(Repo.id == stored.repo_id)).one() == {}
        analysis = session.scalars(select(Analysis).where(Analysis.item_id == item_id)).one()
        assert analysis.status == "completed"


@pytest.mark.parametrize(
    "statement",
    [
        # identity at the top level of content is rejected by the database itself
        "INSERT INTO items (repo_id, kind, external_id, content, content_hash, state) "
        "VALUES (:repo, 'pr', '1', '{\"author\": \"x\"}', :hash, 'open')",
        "INSERT INTO items (repo_id, kind, external_id, content, content_hash, state) "
        "VALUES (:repo, 'commit', '1', '{}', :hash, 'open')",
        "INSERT INTO items (repo_id, kind, external_id, content, content_hash, state) "
        "VALUES (:repo, 'pr', '1', '[]', :hash, 'open')",
    ],
)
def test_database_checks_on_items(session_factory: sessionmaker[Session], statement: str) -> None:
    with session_scope(session_factory) as session:
        repo_id = make_repo(session).id
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        session.execute(text(statement), {"repo": repo_id, "hash": HASH})


def test_confidence_must_be_between_0_and_1(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        repo = make_repo(session)
        item = Item(
            repo_id=repo.id,
            kind="issue",
            external_id="9",
            content={},
            content_hash=HASH,
            state="open",
        )
        session.add(item)
        session.flush()
        item_id = item.id
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        session.add(
            Analysis(item_id=item_id, run_id=uuid.uuid4(), pipeline_version="p", confidence=1.5)
        )


def _add_repo_then_fail(factory: sessionmaker[Session], name: str) -> None:
    with session_scope(factory) as session:
        session.add(Repo(owner="octo", name=name))
        session.flush()
        raise RuntimeError("boom")


def test_session_scope_rolls_back_on_error(session_factory: sessionmaker[Session]) -> None:
    name = f"rollback-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError):
        _add_repo_then_fail(session_factory, name)
    with session_scope(session_factory) as session:
        assert session.scalars(select(Repo).where(Repo.name == name)).first() is None


def test_engine_sessions_use_utc(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SHOW timezone")).scalar_one() == "UTC"

"""Shared fixtures for integration tests that need Postgres.

Each test session works in throwaway databases (``gatekeeper_test_<random>``) created
next to the configured one and dropped afterwards, so local data is never touched.
Locally the tests skip with a clear message when Postgres is unreachable; in CI
(``CI`` is set) they fail instead, so they always run there.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.config import Settings
from gatekeeper.db.session import create_db_engine, create_session_factory, sqlalchemy_url
from gatekeeper.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _unavailable(reason: str) -> None:
    message = f"integration test needs Postgres: {reason}"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def admin_connection() -> Iterator[tuple[URL, Connection]]:
    """Autocommit connection to the server's ``postgres`` database, plus the base URL."""
    try:
        base = sqlalchemy_url(Settings())  # environment first, then .env; never printed
    except ConfigError:
        _unavailable("DATABASE_URL is not set (run `make init-env`)")
        raise  # unreachable; keeps type checkers happy
    engine = create_engine(
        base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"connect_timeout": 3},
    )
    try:
        connection = engine.connect()
    except OperationalError:
        engine.dispose()
        _unavailable(f"no connection to {base.host}:{base.port} (run `make up`)")
        raise
    try:
        yield base, connection
    finally:
        connection.close()
        engine.dispose()


@pytest.fixture(scope="session")
def make_database(admin_connection: tuple[URL, Connection]) -> Iterator[Callable[[], URL]]:
    """Factory for empty throwaway databases, all dropped at session end."""
    base, connection = admin_connection
    created: list[str] = []

    def factory() -> URL:
        name = f"gatekeeper_test_{uuid4().hex[:12]}"
        connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return base.set(database=name)

    yield factory
    for name in created:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def alembic_config(url: URL) -> Config:
    config = Config(toml_file=str(REPO_ROOT / "pyproject.toml"))
    config.attributes["database_url"] = url
    return config


@pytest.fixture(scope="session")
def make_alembic_config() -> Callable[[URL], Config]:
    return alembic_config


@pytest.fixture(scope="session")
def migrated_engine(make_database: Callable[[], URL]) -> Iterator[Engine]:
    url = make_database()
    command.upgrade(alembic_config(url), "head")
    engine = create_db_engine(url=url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)

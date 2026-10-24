"""Engine and session helpers (sync SQLAlchemy 2.x on psycopg 3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import ConfigError

DRIVER = "postgresql+psycopg"


def sqlalchemy_url(settings: Settings) -> URL:
    """Build the SQLAlchemy URL from DATABASE_URL, always using the psycopg 3 driver."""
    settings.require("DATABASE_URL")
    if settings.database_url is None:  # pragma: no cover - require() already raised
        raise ConfigError("missing required setting(s): DATABASE_URL")
    raw = settings.database_url.get_secret_value()
    scheme, sep, rest = raw.partition("://")
    if scheme in {"postgres", "postgresql"}:
        raw = f"{DRIVER}{sep}{rest}"
    return make_url(raw)


def create_db_engine(
    settings: Settings | None = None, *, url: URL | None = None, **kwargs: Any
) -> Engine:
    """Engine with UTC sessions, pre-ping, and bound parameters hidden from error messages.

    ``url`` overrides DATABASE_URL (used by tests and the migration runner).
    """
    url = url if url is not None else sqlalchemy_url(settings or get_settings())
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "hide_parameters": True,
        "connect_args": {
            "options": "-c timezone=UTC",
            "application_name": "gatekeeper",
            "connect_timeout": 10,
        },
    }
    options.update(kwargs)
    return create_engine(url, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception, always close."""
    with factory.begin() as session:
        yield session

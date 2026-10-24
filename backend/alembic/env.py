"""Alembic environment.

The database URL comes from Settings (``DATABASE_URL``, psycopg 3 driver) and is never
written into the Alembic config, so it cannot leak through config dumps. Tests pass an
explicit URL via ``config.attributes["database_url"]``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import URL, pool

from gatekeeper.config import get_settings
from gatekeeper.db.models import Base
from gatekeeper.db.session import create_db_engine, sqlalchemy_url
from gatekeeper.logging import configure_logging

config = context.config
target_metadata = Base.metadata


def _url_override() -> URL | None:
    value = config.attributes.get("database_url")
    return value if isinstance(value, URL) else None


def run_migrations_offline() -> None:
    context.configure(
        url=_url_override() or sqlalchemy_url(get_settings()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(url=_url_override(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if config.cmd_opts is not None:  # invoked from the alembic CLI, not from tests
    configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

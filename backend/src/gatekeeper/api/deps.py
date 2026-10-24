"""Default production wiring for the gateway (database store and readiness checks)."""

from __future__ import annotations

from collections.abc import Callable

import redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from gatekeeper.api.store import PostgresWebhookStore
from gatekeeper.config import Settings
from gatekeeper.db.session import create_db_engine, create_session_factory


def default_store(settings: Settings) -> PostgresWebhookStore:
    factory = create_session_factory(create_db_engine(settings))
    return PostgresWebhookStore(factory, max_attempts=settings.job_max_attempts)


def default_readiness(settings: Settings) -> Callable[[], dict[str, str]]:
    engine = create_db_engine(settings, connect_args={"connect_timeout": 3})
    client = (
        redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
        if settings.redis_url
        else None
    )

    def check() -> dict[str, str]:
        components: dict[str, str] = {}
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            components["database"] = "ok"
        except SQLAlchemyError:
            components["database"] = "error"
        if client is None:
            components["redis"] = "not configured"
        else:
            try:
                client.ping()
                components["redis"] = "ok"
            except (RedisError, OSError):
                components["redis"] = "error"
        return components

    return check

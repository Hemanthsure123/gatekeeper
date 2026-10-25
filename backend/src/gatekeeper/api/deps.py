"""Default production wiring: database store, readiness checks, extension services."""

from __future__ import annotations

from collections.abc import Callable

import redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from gatekeeper.activity import ActivityFeed
from gatekeeper.api.extension import ExtensionDeps
from gatekeeper.api.store import PostgresWebhookStore
from gatekeeper.audit import AuditRecorder, Redactor
from gatekeeper.config import Settings
from gatekeeper.db.session import create_db_engine, create_session_factory
from gatekeeper.github.client import GitHubClient
from gatekeeper.models.factory import get_backend


def default_store(settings: Settings) -> PostgresWebhookStore:
    factory = create_session_factory(create_db_engine(settings))
    return PostgresWebhookStore(factory, max_attempts=settings.job_max_attempts)


def default_extension(settings: Settings) -> ExtensionDeps:
    factory = create_session_factory(create_db_engine(settings))
    redactor = Redactor.from_settings(settings)
    return ExtensionDeps(
        settings=settings,
        session_factory=factory,
        github=GitHubClient(settings),
        recorder=AuditRecorder(factory, redactor),
        activity=ActivityFeed(factory, redactor),
        model_factory=lambda: get_backend(settings),
    )


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

"""Unit tests for the database models and session helpers (no database needed)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr

from gatekeeper.config import Settings
from gatekeeper.db import Base, Item
from gatekeeper.db.models import IDENTITY_KEYS, find_identity_keys
from gatekeeper.db.session import create_db_engine, sqlalchemy_url
from gatekeeper.errors import ConfigError, ValidationFailed


def settings(database_url: str | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(database_url) if database_url else None,
    )


def test_exactly_the_core_tables() -> None:
    assert set(Base.metadata.tables) == {"repos", "items", "item_authors", "analyses", "events"}


def test_constraint_names_follow_convention() -> None:
    items = Base.metadata.tables["items"]
    names = {c.name for c in items.constraints}
    assert {
        "pk_items",
        "fk_items_repo_id_repos",
        "uq_items_repo_id_kind_external_id",
        "ck_items_kind",
        "ck_items_content_identity_free",
    } <= names


def test_find_identity_keys_is_recursive_and_case_insensitive() -> None:
    content = {
        "title": "Fix parser",
        "files": [{"path": "a.py", "Author": "x"}],
        "meta": {"labels": ["bug"], "user": {"id": 1}},
    }
    assert find_identity_keys(content) == ["content.files[0].Author", "content.meta.user"]
    assert find_identity_keys({"title": "t", "body": "uses login() helper"}) == []


@pytest.mark.parametrize("key", sorted(IDENTITY_KEYS))
def test_item_rejects_identity_fields(key: str) -> None:
    with pytest.raises(ValidationFailed) as caught:
        Item(content={"title": "x", "nested": {key: "someone"}})
    assert caught.value.details == {"paths": [f"content.nested.{key}"]}


def test_item_accepts_identity_free_content() -> None:
    item = Item(repo_id=uuid.uuid4(), kind="pr", content={"title": "x", "diff_stats": {"files": 2}})
    assert item.content["title"] == "x"


@pytest.mark.parametrize(
    "raw",
    ["postgresql://u:p@127.0.0.1:15432/db", "postgres://u:p@127.0.0.1:15432/db"],
)
def test_sqlalchemy_url_forces_psycopg_driver(raw: str) -> None:
    url = sqlalchemy_url(settings(raw))
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "db"
    assert "p@" not in repr(url)  # SQLAlchemy masks the password in reprs


def test_sqlalchemy_url_requires_database_url() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        sqlalchemy_url(settings(None))


def test_engine_hides_parameters_and_uses_utc() -> None:
    engine = create_db_engine(settings("postgresql://u:p@127.0.0.1:15432/db"))
    try:
        assert engine.hide_parameters is True
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()

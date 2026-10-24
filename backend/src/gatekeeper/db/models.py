"""Core schema: repos, items, item_authors, analyses and the append-only events table.

Identity rule (CLAUDE.md, rule a): ``items.content`` never contains author or other
identity fields. Identity lives only in ``item_authors``, and the review core must
never read that table; a later prompt enforces this structurally.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Double,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from gatekeeper.db.base import Base, CreatedAt, Timestamps, UuidPrimaryKey
from gatekeeper.errors import ValidationFailed


class ItemKind(StrEnum):
    PR = "pr"
    ISSUE = "issue"
    COMMENT = "comment"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABSTAINED = "abstained"


# Mirrors gatekeeper.audit.types.Actor (a test keeps them in sync). Kept here so the
# models never import the audit package, which itself depends on the models.
EVENT_ACTORS: Final = ("system", "model", "maintainer", "github")
EVENT_SCHEMA_VERSION: Final = "gatekeeper.event/1"

# Keys that identify a person. Rejected anywhere inside items.content (ORM check) and
# at the top level by a database CHECK constraint as a safety net.
IDENTITY_KEYS: Final = frozenset(
    {
        "author",
        "user",
        "sender",
        "committer",
        "login",
        "email",
        "user_id",
        "avatar_url",
        "gravatar_id",
        "author_association",
        "account_created_at",
        "is_first_contribution",
    }
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def identity_keys_check() -> str:
    """SQL for the top-level identity-key CHECK on items.content (also used by migration 0001)."""
    keys = ", ".join(f"'{key}'" for key in sorted(IDENTITY_KEYS))
    return f"jsonb_typeof(content) = 'object' AND NOT (content ?| ARRAY[{keys}])"


def find_identity_keys(value: Any, path: str = "content") -> list[str]:
    """Return the paths of every identity key nested anywhere in ``value``."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}"
            if str(key).lower() in IDENTITY_KEYS:
                found.append(here)
            found.extend(find_identity_keys(item, here))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(find_identity_keys(item, f"{path}[{index}]"))
    return found


class Repo(UuidPrimaryKey, Timestamps, Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("owner", "name"),)

    github_repo_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    owner: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    config: Mapped[dict[str, Any]] = mapped_column(
        default=lambda: {}, server_default=text("'{}'::jsonb")
    )


class Item(UuidPrimaryKey, Timestamps, Base):
    """A PR, issue or comment. ``content`` is identity-free by construction."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("repo_id", "kind", "external_id"),
        CheckConstraint(_in("kind", [k.value for k in ItemKind]), name="kind"),
        CheckConstraint(identity_keys_check(), name="content_identity_free"),
        CheckConstraint("char_length(content_hash) = 64", name="content_hash_sha256"),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]]
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(Text)

    @validates("content")
    def _reject_identity(self, _key: str, value: dict[str, Any]) -> dict[str, Any]:
        found = find_identity_keys(value)
        if found:
            raise ValidationFailed(
                "item content must not contain identity fields; store them in item_authors",
                details={"paths": found},
            )
        return value


class ItemAuthor(CreatedAt, Base):
    """The ONLY place author identity lives. The review core must never read this table."""

    __tablename__ = "item_authors"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    login: Mapped[str] = mapped_column(Text)
    github_user_id: Mapped[int] = mapped_column(BigInteger)
    account_created_at: Mapped[datetime | None]
    author_association: Mapped[str | None] = mapped_column(Text)
    is_first_contribution: Mapped[bool | None]


class Analysis(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(_in("status", [s.value for s in AnalysisStatus]), name="status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    pipeline_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, default=AnalysisStatus.QUEUED.value, server_default=AnalysisStatus.QUEUED.value
    )
    result: Mapped[dict[str, Any] | None]
    confidence: Mapped[float | None] = mapped_column(Double)
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Event(CreatedAt, Base):
    """Append-only audit event. A database trigger rejects UPDATE, DELETE and TRUNCATE."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(
            "parent_seq IS NULL OR (parent_seq >= 1 AND parent_seq < seq)",
            name="parent_before_child",
        ),
        CheckConstraint(_in("actor", EVENT_ACTORS), name="actor"),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy mapper config

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID]
    seq: Mapped[int] = mapped_column(Integer)
    parent_seq: Mapped[int | None] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), index=True
    )
    type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    payload_hash: Mapped[str] = mapped_column(String(64))
    chain_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(
        Text, default=EVENT_SCHEMA_VERSION, server_default=EVENT_SCHEMA_VERSION
    )

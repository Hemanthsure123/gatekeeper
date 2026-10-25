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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from gatekeeper.db.base import Base, CreatedAt, Timestamps, UuidPrimaryKey
from gatekeeper.errors import ValidationFailed


class ItemKind(StrEnum):
    PR = "pr"
    ISSUE = "issue"
    COMMENT = "comment"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


ACTIVE_JOB_STATUSES: Final = ("queued", "running")
DELIVERY_STATUSES: Final = ("received", "queued", "ignored", "enqueue_failed")


class Verdict(StrEnum):
    LOOKS_VALUABLE = "looks_valuable"
    NEEDS_HUMAN_LOOK = "needs_human_look"
    LIKELY_LOW_VALUE = "likely_low_value"


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
ITEM_AUTHORS_COMMENT: Final = (
    "The only place author identity lives. The review core must never read this table."
)

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
    # GitHub owner/repo names are case-insensitive: unique on lower(), display casing kept.
    __table_args__ = (
        Index(
            "uq_repos_owner_name_ci",
            func.lower(text("owner")),
            func.lower(text("name")),
            unique=True,
        ),
    )

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
    __table_args__ = ({"comment": ITEM_AUTHORS_COMMENT},)

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
        CheckConstraint(_in("verdict", [v.value for v in Verdict]), name="verdict"),
        Index("ix_analyses_item_id_created_at", "item_id", "created_at"),
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
    #: Denormalised from the run for listing and skip-unchanged checks.
    head_sha: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(Text)


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


class WebhookDelivery(Timestamps, Base):
    """One GitHub webhook delivery, keyed by ``X-GitHub-Delivery`` for idempotency."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),)

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    repo_full_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)


class Job(UuidPrimaryKey, Timestamps, Base):
    """A unit of background work in the Postgres-backed queue (see ADR 0004)."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", [s.value for s in JobStatus]), name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
        Index(
            "uq_jobs_dedupe_key_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(
        Text, default=JobStatus.QUEUED.value, server_default=JobStatus.QUEUED.value
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None]
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None]


ACTIVITY_LEVELS: Final = ("info", "warn", "error")
CHAT_ROLES: Final = ("user", "assistant")


class ActivityEvent(Base):
    """Display-only live feed for the extension (``gatekeeper.activity``). The audit
    trail (``events``) stays the source of truth; this table is best effort."""

    __tablename__ = "activity_events"
    __table_args__ = (
        CheckConstraint(_in("level", ACTIVITY_LEVELS), name="level"),
        Index("ix_activity_events_analysis_id_id", "analysis_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    step: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text, default="info", server_default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(
        default=lambda: {}, server_default=text("'{}'::jsonb")
    )


class ChatMessage(UuidPrimaryKey, CreatedAt, Base):
    """One chat turn about a PR's review. Identity-free: user text is scrubbed first."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(_in("role", CHAT_ROLES), name="role"),
        Index("ix_chat_messages_item_id_created_at", "item_id", "created_at"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class CommentPost(Base):
    """The single GitHub write: one approved comment per analysis (idempotency key)."""

    __tablename__ = "comment_posts"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="RESTRICT"), primary_key=True
    )
    github_comment_id: Mapped[int] = mapped_column(BigInteger)
    html_url: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(server_default=func.now())

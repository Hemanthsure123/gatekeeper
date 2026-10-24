"""Initial schema: repos, items, item_authors, analyses, append-only events.

Revision ID: 0001
Revises:
Create Date: 2026-10-25

Migrations are frozen history: values that also exist in application code (identity
keys, enum values) are copied here on purpose so later code changes cannot rewrite
what this revision created.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY_KEYS = (
    "account_created_at",
    "author",
    "author_association",
    "avatar_url",
    "committer",
    "email",
    "gravatar_id",
    "is_first_contribution",
    "login",
    "sender",
    "user",
    "user_id",
)
APPEND_ONLY_FUNCTION = "events_reject_mutation"


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "repos",
        _uuid_pk(),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repos")),
        sa.UniqueConstraint("github_repo_id", name=op.f("uq_repos_github_repo_id")),
        sa.UniqueConstraint("owner", "name", name=op.f("uq_repos_owner_name")),
    )

    identity_keys = ", ".join(f"'{key}'" for key in IDENTITY_KEYS)
    op.create_table(
        "items",
        _uuid_pk(),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_items_repo_id_repos"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "repo_id", "kind", "external_id", name=op.f("uq_items_repo_id_kind_external_id")
        ),
        sa.CheckConstraint("kind IN ('pr', 'issue', 'comment')", name=op.f("ck_items_kind")),
        sa.CheckConstraint(
            f"jsonb_typeof(content) = 'object' AND NOT (content ?| ARRAY[{identity_keys}])",
            name=op.f("ck_items_content_identity_free"),
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name=op.f("ck_items_content_hash_sha256")
        ),
    )

    op.create_table(
        "item_authors",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        _ts("account_created_at", nullable=True, default=False),
        sa.Column("author_association", sa.Text(), nullable=True),
        sa.Column("is_first_contribution", sa.Boolean(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_item_authors")),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_item_authors_item_id_items"),
            ondelete="CASCADE",
        ),
    )
    op.execute(
        "COMMENT ON TABLE item_authors IS "
        "'The only place author identity lives. The review core must never read this table.'"
    )

    op.create_table(
        "analyses",
        _uuid_pk(),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("started_at", nullable=True, default=False),
        _ts("finished_at", nullable=True, default=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyses")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_analyses_item_id_items"), ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'abstained')",
            name=op.f("ck_analyses_status"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_analyses_confidence_range")
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_analyses_finished_after_started"),
        ),
    )
    op.create_index(op.f("ix_analyses_item_id"), "analyses", ["item_id"])
    op.create_index(op.f("ix_analyses_run_id"), "analyses", ["run_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),  # BIGSERIAL
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("parent_seq", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("chain_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Text(), server_default="gatekeeper.event/1", nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_events_item_id_items"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_events_run_id_seq")),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_events_seq_positive")),
        sa.CheckConstraint(
            "parent_seq IS NULL OR (parent_seq >= 1 AND parent_seq < seq)",
            name=op.f("ck_events_parent_before_child"),
        ),
        sa.CheckConstraint(
            "actor IN ('system', 'model', 'maintainer', 'github')", name=op.f("ck_events_actor")
        ),
    )
    # The unique (run_id, seq) index already serves lookups by run_id.
    op.create_index(op.f("ix_events_item_id"), "events", ["item_id"])

    # Append-only: reject UPDATE, DELETE and TRUNCATE. Any future retention job needs an
    # explicit, audited migration path; it must never just disable this trigger.
    op.execute(
        f"""
        CREATE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'events is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events "
        f"FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
    )
    op.execute(
        "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_no_truncate ON events")
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON events")
    op.execute(f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()")
    op.drop_index(op.f("ix_events_item_id"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_analyses_run_id"), table_name="analyses")
    op.drop_index(op.f("ix_analyses_item_id"), table_name="analyses")
    op.drop_table("analyses")
    op.drop_table("item_authors")
    op.drop_table("items")
    op.drop_table("repos")

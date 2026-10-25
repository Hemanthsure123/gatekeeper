"""Extension tables: the live activity feed, chat messages and approved comment posts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),  # BIGSERIAL
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), server_default="info", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_events")),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_activity_events_analysis_id_analyses"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "level IN ('info', 'warn', 'error')", name=op.f("ck_activity_events_level")
        ),
    )
    op.create_index("ix_activity_events_analysis_id_id", "activity_events", ["analysis_id", "id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_chat_messages_item_id_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_chat_messages_analysis_id_analyses"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_chat_messages_role")),
    )
    op.create_index(
        "ix_chat_messages_item_id_created_at", "chat_messages", ["item_id", "created_at"]
    )

    op.create_table(
        "comment_posts",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=False),
        sa.Column("html_url", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.Text(), nullable=False),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("analysis_id", name=op.f("pk_comment_posts")),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_comment_posts_analysis_id_analyses"),
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("comment_posts")
    op.drop_index("ix_chat_messages_item_id_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_activity_events_analysis_id_id", table_name="activity_events")
    op.drop_table("activity_events")

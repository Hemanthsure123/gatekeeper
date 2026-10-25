"""Denormalised review columns on analyses: head_sha, content_hash, verdict.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("head_sha", sa.Text(), nullable=True))
    op.add_column("analyses", sa.Column("content_hash", sa.Text(), nullable=True))
    op.add_column("analyses", sa.Column("verdict", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_analyses_verdict"),
        "analyses",
        "verdict IN ('looks_valuable', 'needs_human_look', 'likely_low_value')",
    )
    op.create_index("ix_analyses_item_id_created_at", "analyses", ["item_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_analyses_item_id_created_at", table_name="analyses")
    op.drop_constraint(op.f("ck_analyses_verdict"), "analyses", type_="check")
    op.drop_column("analyses", "verdict")
    op.drop_column("analyses", "content_hash")
    op.drop_column("analyses", "head_sha")

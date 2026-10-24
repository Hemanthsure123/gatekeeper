"""Make repo owner/name unique case-insensitively, as GitHub treats them.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_repos_owner_name"), "repos", type_="unique")
    op.execute("CREATE UNIQUE INDEX uq_repos_owner_name_ci ON repos (lower(owner), lower(name))")


def downgrade() -> None:
    op.execute("DROP INDEX uq_repos_owner_name_ci")
    op.create_unique_constraint(op.f("uq_repos_owner_name"), "repos", ["owner", "name"])

"""add one-time fresher classification to jobs

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("is_fresher_role", sa.Boolean(), nullable=True))
    op.add_column("jobs", sa.Column("fresher_reason", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("classified_by", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_is_fresher_role", "jobs", ["is_fresher_role"])


def downgrade() -> None:
    op.drop_index("ix_jobs_is_fresher_role", table_name="jobs")
    op.drop_column("jobs", "classified_at")
    op.drop_column("jobs", "classified_by")
    op.drop_column("jobs", "fresher_reason")
    op.drop_column("jobs", "is_fresher_role")

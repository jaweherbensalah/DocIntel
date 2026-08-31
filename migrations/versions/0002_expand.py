"""expand: add the normalised tables alongside the JSON payload

Additive only, so it can be applied while the previous release is still serving
traffic.

Revision ID: 0002_expand
Revises: 0001_baseline
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_expand"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# JSONB on Postgres, plain JSON elsewhere; mirrors app.models.JsonColumn.
JSON_COLUMN = sa.JSON().with_variant(postgresql.JSONB, "postgresql")

def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("result_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["result_id"], ["results.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("result_id", name="uq_profiles_result_id"),
    )
    op.create_index("ix_profiles_years_experience", "profiles", ["years_experience"])

    op.create_table(
        "profile_skills",
        sa.Column("profile_id", sa.Integer(), primary_key=True),
        sa.Column("skill", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_profile_skills_skill", "profile_skills", ["skill"])

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("result_id", sa.String(32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("candidate_name", sa.String(200), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("job_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("matched_skills", JSON_COLUMN, nullable=True),
        sa.Column("missing_skills", JSON_COLUMN, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["result_id"], ["results.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_matches_result_id", "matches", ["result_id"])
    op.create_index("ix_matches_score", "matches", ["score"])

    # `results` is live, so this index must not lock it. CONCURRENTLY cannot
    # run inside a transaction.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_kind_status "
                "ON results (kind, status)"
            )
    else:
        op.create_index("ix_results_kind_status", "results", ["kind", "status"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_results_kind_status")
    else:
        op.drop_index("ix_results_kind_status", table_name="results")

    op.drop_table("matches")
    op.drop_table("profile_skills")
    op.drop_table("profiles")

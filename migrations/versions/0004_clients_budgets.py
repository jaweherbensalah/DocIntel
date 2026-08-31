"""clients, budgets and usage events

Revision ID: 0004_clients_budgets
Revises: 0003_backfill
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_clients_budgets"
down_revision: Union[str, None] = "0003_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column(
            "monthly_budget_cents", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("api_key_hash", name="uq_clients_api_key_hash"),
    )
    op.create_index("ix_clients_api_key_hash", "clients", ["api_key_hash"])

    op.create_table(
        "budget_periods",
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("period", sa.String(7), primary_key=True),
        sa.Column("reserved_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spent_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_id", sa.String(32), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("estimated_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_cents", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_usage_events_client_id", "usage_events", ["client_id"])


def downgrade() -> None:
    op.drop_table("usage_events")
    op.drop_table("budget_periods")
    op.drop_index("ix_clients_api_key_hash", table_name="clients")
    op.drop_table("clients")

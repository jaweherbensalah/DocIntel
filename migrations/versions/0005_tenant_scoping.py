"""tenant scoping: give every result an owning client

Revision ID: 0005_tenant_scoping
Revises: 0004_clients_budgets
Create Date: 2026-08-31
"""

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_tenant_scoping"
down_revision: Union[str, None] = "0004_clients_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.tenancy")


def upgrade() -> None:
    # Nullable with no default, so Postgres rewrites no rows and takes no
    # meaningful lock.
    op.add_column("results", sa.Column("client_id", sa.String(32), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_client_id "
                "ON results (client_id)"
            )
    else:
        op.create_index("ix_results_client_id", "results", ["client_id"])

    # Results predate tenancy. If the service only ever had one client, they are
    # unambiguously theirs; otherwise ownership is unknowable and they stay
    # NULL, which every query treats as invisible.
    clients = bind.execute(sa.text("SELECT id FROM clients")).scalars().all()
    if len(clients) == 1:
        bind.execute(
            sa.text("UPDATE results SET client_id = :cid WHERE client_id IS NULL"),
            {"cid": clients[0]},
        )
    else:
        orphans = bind.execute(
            sa.text("SELECT COUNT(*) FROM results WHERE client_id IS NULL")
        ).scalar_one()
        if orphans:
            logger.warning(
                "%s results left unowned; %s clients exist so ownership is ambiguous",
                orphans,
                len(clients),
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_results_client_id")
    else:
        op.drop_index("ix_results_client_id", table_name="results")
    op.drop_column("results", "client_id")

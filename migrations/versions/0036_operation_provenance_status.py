"""Distinguish post-ledger operations from preserved legacy operations.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.add_column(
            sa.Column(
                "provenance_status",
                sa.String(length=32),
                server_default="legacy_preledger",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_operations_provenance_status",
            "provenance_status IN ('complete', 'legacy_preledger')",
        )
        batch.create_check_constraint(
            "ck_operations_complete_changeset",
            "provenance_status = 'legacy_preledger' OR "
            "originating_changeset_ref IS NOT NULL",
        )
    operations = sa.table(
        "operations",
        sa.column("originating_changeset_ref", sa.String()),
        sa.column("provenance_status", sa.String()),
    )
    op.execute(
        operations.update()
        .where(operations.c.originating_changeset_ref.is_not(None))
        .values(provenance_status="complete")
    )


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("ck_operations_complete_changeset", type_="check")
        batch.drop_constraint("ck_operations_provenance_status", type_="check")
        batch.drop_column("provenance_status")

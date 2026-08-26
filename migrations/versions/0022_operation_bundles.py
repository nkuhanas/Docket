"""Add durable conflict-resolution operation bundles.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_bundles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_revision_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial_failed', 'failed', "
            "'reconciliation_required')",
            name="ck_operation_bundles_status",
        ),
        sa.CheckConstraint(
            "resolution IN ('keep_both', 'new_wins', 'keep_existing')",
            name="ck_operation_bundles_resolution",
        ),
        sa.ForeignKeyConstraint(
            ["action_revision_id"], ["action_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("operations") as batch:
        batch.add_column(sa.Column("bundle_id", sa.Uuid()))
        batch.add_column(sa.Column("predecessor_operation_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_operations_bundle",
            "operation_bundles",
            ["bundle_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_operations_predecessor",
            "operations",
            ["predecessor_operation_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("fk_operations_predecessor", type_="foreignkey")
        batch.drop_constraint("fk_operations_bundle", type_="foreignkey")
        batch.drop_column("predecessor_operation_id")
        batch.drop_column("bundle_id")
    op.drop_table("operation_bundles")

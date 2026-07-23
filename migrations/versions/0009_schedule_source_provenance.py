"""Allow one trusted schedule request to source multiple records.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    naming_convention = (
        {"uq": "uq_%(table_name)s_%(column_0_name)s"} if dialect == "sqlite" else None
    )
    old_constraint = (
        "uq_record_sources_source_request_key"
        if dialect == "sqlite"
        else "record_sources_source_request_key_key"
    )
    with op.batch_alter_table(
        "record_sources",
        naming_convention=naming_convention,
    ) as batch:
        batch.drop_constraint(
            old_constraint,
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_record_sources_record_request",
            ["record_id", "source_request_key"],
        )
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status", type_="check")
        batch.create_check_constraint(
            "ck_actions_status",
            "status IN ('available', 'approval_pending', 'ready', 'executing', "
            "'succeeded', 'partial_failed', 'rejected', 'expired', 'superseded', "
            "'failed', 'reconciliation_required')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    actions = sa.table("actions", sa.column("status", sa.String()))
    connection.execute(
        actions.update().where(actions.c.status == "partial_failed").values(status="failed")
    )
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status", type_="check")
        batch.create_check_constraint(
            "ck_actions_status",
            "status IN ('available', 'approval_pending', 'ready', 'executing', "
            "'succeeded', 'rejected', 'expired', 'superseded', 'failed', "
            "'reconciliation_required')",
        )
    with op.batch_alter_table("record_sources") as batch:
        batch.drop_constraint(
            "uq_record_sources_record_request",
            type_="unique",
        )
        batch.create_unique_constraint(
            "record_sources_source_request_key_key",
            ["source_request_key"],
        )

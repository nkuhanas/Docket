"""Complete phase-one runtime diagnostics and provenance inspection storage.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_log_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=128), nullable=False),
        sa.Column("event_code", sa.String(length=128), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("related_refs", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('debug', 'info', 'warning', 'error', 'critical')",
            name="ck_runtime_log_entries_severity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_runtime_log_entries_component_occurred",
        "runtime_log_entries",
        ["component", "occurred_at"],
    )
    op.create_index(
        "ix_runtime_log_entries_event_occurred",
        "runtime_log_entries",
        ["event_code", "occurred_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_runtime_log_entries_immutable
            BEFORE UPDATE OR DELETE ON runtime_log_entries
            FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_provenance()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_runtime_log_entries_immutable "
            "ON runtime_log_entries"
        )
    op.drop_index(
        "ix_runtime_log_entries_event_occurred",
        table_name="runtime_log_entries",
    )
    op.drop_index(
        "ix_runtime_log_entries_component_occurred",
        table_name="runtime_log_entries",
    )
    op.drop_table("runtime_log_entries")

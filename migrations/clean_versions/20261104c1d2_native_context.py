"""retain owner-bound native context and exact prepared proposals

Revision ID: 20261104c1d2
Revises: 20261028b2c3
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104c1d2"
down_revision = "20261028b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_contexts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), unique=True, nullable=False),
        sa.Column("principal_ref", sa.String(255), nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("versions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_native_contexts_principal_ref", "native_contexts", ["principal_ref"])
    op.create_table(
        "prepared_commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), unique=True, nullable=False),
        sa.Column("principal_ref", sa.String(255), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("command", sa.String(128), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("context_ref", sa.String(40), sa.ForeignKey("native_contexts.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_ref", sa.String(40), sa.ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("changeset_ref", sa.String(40), sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("principal_ref", "operation_id", name="uq_prepared_command_operation"),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in ("native_contexts", "prepared_commands"):
            op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()")


def downgrade() -> None:
    for table in ("prepared_commands", "native_contexts"):
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained native contexts/proposals")
    op.drop_table("prepared_commands")
    op.drop_index("ix_native_contexts_principal_ref", table_name="native_contexts")
    op.drop_table("native_contexts")

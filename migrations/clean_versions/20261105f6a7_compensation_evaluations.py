"""retain exact owner-bound compensation calculation snapshots

Revision ID: 20261105f6a7
Revises: 20261105e5f6
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105f6a7"
down_revision = "20261105e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compensation_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("principal_ref", sa.String(255), nullable=False),
        sa.Column(
            "work_report_ref",
            sa.String(40),
            sa.ForeignKey("work_report_snapshots.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("calculation_version", sa.String(64), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_compensation_evaluations_principal_ref", "compensation_evaluations", ["principal_ref"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE TRIGGER trg_compensation_evaluations_immutable
            BEFORE UPDATE OR DELETE ON compensation_evaluations FOR EACH ROW
            EXECUTE FUNCTION docket_reject_immutable_row()""")


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM compensation_evaluations)"))
        .scalar()
    ):
        raise RuntimeError("Downgrade cannot discard retained compensation evaluations")
    op.drop_table("compensation_evaluations")

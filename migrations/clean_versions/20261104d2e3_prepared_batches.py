"""retain exact prepared batch consumption without rewriting original proposals

Revision ID: 20261104d2e3
Revises: 20261104c1d2
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104d2e3"
down_revision = "20261104c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepared_consumptions",
        sa.Column(
            "member_ref",
            sa.String(40),
            sa.ForeignKey("prepared_commands.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "batch_ref",
            sa.String(40),
            sa.ForeignKey("prepared_commands.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_prepared_consumption_immutable BEFORE UPDATE OR DELETE ON prepared_consumptions FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM prepared_consumptions)"))
        .scalar()
    ):
        raise RuntimeError("Downgrade cannot discard prepared batch receipts")
    op.drop_table("prepared_consumptions")

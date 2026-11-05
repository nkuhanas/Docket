"""retain native workflow receipts without plaintext argument logging

Revision ID: 20261104e3f4
Revises: 20261104d2e3
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104e3f4"
down_revision = "20261104d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_outcomes",
        sa.Column(
            "request_ref",
            sa.String(40),
            sa.ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("command", sa.String(128), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_native_outcome_immutable BEFORE UPDATE OR DELETE ON native_outcomes FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM native_outcomes)")).scalar():
        raise RuntimeError("Downgrade cannot discard native command receipts")
    op.drop_table("native_outcomes")

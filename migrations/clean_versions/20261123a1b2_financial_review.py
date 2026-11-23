"""Reconciliation and reviewed workpaper revisions. Existing finances need no backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20261123a1b2"
down_revision = "20261122b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_review_facts",
        sa.Column("ref_id", sa.String(40), primary_key=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "prior_ref", sa.String(40), sa.ForeignKey("financial_review_facts.ref_id"), unique=True
        ),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column(
            "changeset_ref", sa.String(40), sa.ForeignKey("change_sets.ref_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operator_ref", "kind", "identity_key", "revision"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_financial_review_immutable BEFORE UPDATE OR DELETE ON "
            "financial_review_facts FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    op.create_index(
        "ix_financial_review_owner_kind", "financial_review_facts", ["operator_ref", "kind"]
    )


def downgrade() -> None:
    reports = sa.table("financial_report_snapshots", sa.column("parameters_json", sa.JSON))
    retained_report = (
        sa.select(sa.literal(1))
        .select_from(reports)
        .where(reports.c.parameters_json["report_kind"].as_string() == "financial_year")
        .limit(1)
    )
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM financial_review_facts")
    ) or op.get_bind().scalar(retained_report):
        raise RuntimeError("Retained financial workpapers prevent downgrade")
    op.drop_table("financial_review_facts")

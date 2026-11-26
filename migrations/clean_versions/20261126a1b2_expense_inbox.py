"""Record agent expense review decisions and project the outstanding state."""

import sqlalchemy as sa
from alembic import op

revision = "20261126a1b2"
down_revision = "20261123b2c3"
branch_labels = None
depends_on = None

TABLE = "financial_evidence_candidates"


def upgrade() -> None:
    op.add_column(
        TABLE, sa.Column("route_attempts", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column(TABLE, sa.Column("retry_at", sa.DateTime(timezone=True)))
    op.add_column(
        TABLE,
        sa.Column("review_state", sa.String(24), nullable=False, server_default="needs_review"),
    )
    op.execute(f"""UPDATE {TABLE} SET review_state = CASE
      WHEN disposition = 'new_expense_recorded' THEN 'booked' ELSE 'linked' END
      WHERE expense_ref IS NOT NULL""")
    op.create_index("ix_financial_evidence_candidates_review_state", TABLE, ["review_state"])
    op.create_table(
        "financial_candidate_decisions",
        sa.Column(
            "candidate_ref", sa.String(40), sa.ForeignKey(TABLE + ".ref_id"), primary_key=True
        ),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("note", sa.String(1000)),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("changeset_ref", sa.String(40), sa.ForeignKey("change_sets.ref_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_candidate_decision_immutable BEFORE UPDATE OR DELETE ON "
            "financial_candidate_decisions FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM financial_candidate_decisions")):
        raise RuntimeError("Retained candidate decisions prevent downgrade")
    op.drop_table("financial_candidate_decisions")
    op.drop_index("ix_financial_evidence_candidates_review_state", table_name=TABLE)
    op.drop_column(TABLE, "review_state")
    op.drop_column(TABLE, "route_attempts")
    op.drop_column(TABLE, "retry_at")

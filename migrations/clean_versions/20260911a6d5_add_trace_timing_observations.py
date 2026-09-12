"""retain closed gateway timing observations without payloads

Revision ID: 20260911a6d5
Revises: 20260911f5c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911a6d5"
down_revision: str | None = "20260911f5c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trace_timing_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("trace_ref", sa.String(40), sa.ForeignKey(
            "conversational_tool_traces.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('model_request', 'context_schema', 'local_validation')",
            name="ck_trace_timing_observations_phase"),
        sa.CheckConstraint("ended_at >= started_at", name="ck_trace_timing_observations_order"),
    )
    op.create_index("ix_trace_timing_observations_trace_ref", "trace_timing_observations", ["trace_ref"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_trace_timing_observations_immutable BEFORE UPDATE OR DELETE "
            "ON trace_timing_observations FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM trace_timing_observations)")):
        raise RuntimeError("Trace timings exist; downgrade would discard observations")
    op.drop_table("trace_timing_observations")

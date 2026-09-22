"""bind clarification replies to retained request evidence, without backfill

Revision ID: 20260922d0b9
Revises: 20260921c9a8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922d0b9"
down_revision: str | None = "20260921c9a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "clarification_replies"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("utterance_ref", sa.String(40), sa.ForeignKey(
            "operator_utterances.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("projection_ref", sa.String(40), sa.ForeignKey(
            "operator_projections.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("intent_session_ref", sa.String(40), sa.ForeignKey(
            "intent_sessions.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("selected_option_ref", sa.String(40), sa.ForeignKey(
            "persisted_semantic_options.ref_id", ondelete="RESTRICT")),
        sa.Column("evidence_utterance_refs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clarification_replies_intent_session_ref", _TABLE, ["intent_session_ref"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")):
        raise RuntimeError("Clarification replies exist; downgrade would discard evidence")
    op.drop_table(_TABLE)

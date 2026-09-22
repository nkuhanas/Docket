"""retain mixed-source field evidence without rewriting historical authority

Revision ID: 20260921c9a8
Revises: 20260912b8f7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921c9a8"
down_revision: str | None = "20260912b8f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "request_field_evidence"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("semantic_request_ref", sa.String(40), sa.ForeignKey(
            "semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("proof_json", sa.JSON(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    # Deliberately no request inference, backfill, replay or provider effects.


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")):
        raise RuntimeError("Field evidence exists; downgrade would discard evidence")
    op.drop_table(_TABLE)

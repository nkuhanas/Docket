"""preserve immutable one-time request adoption proofs

Revision ID: 20260911f5c4
Revises: 20260911e4b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911f5c4"
down_revision: str | None = "20260911e4b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "request_assembly_adoptions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("semantic_request_ref", sa.String(40), sa.ForeignKey(
            "semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("change_set_id", sa.Uuid(), sa.ForeignKey(
            "change_sets.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("original_revision_id", sa.Uuid(), sa.ForeignKey(
            "change_set_revisions.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("adopted_revision_id", sa.Uuid(), sa.ForeignKey(
            "change_set_revisions.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("proof_json", sa.JSON(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    # No adoption, request replay, authority rewrite or domain backfill at startup.


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")):
        raise RuntimeError("Request adoption proofs exist; downgrade would discard evidence")
    op.drop_table(_TABLE)

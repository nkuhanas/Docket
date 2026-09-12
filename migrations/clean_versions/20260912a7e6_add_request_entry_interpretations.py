"""preserve selected source interpretations independently from staged repairs

Revision ID: 20260912a7e6
Revises: 20260911a6d5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260912a7e6"
down_revision: str | None = "20260911a6d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "request_entry_interpretations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("semantic_request_ref", sa.String(40), sa.ForeignKey(
            "semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("entry_id", sa.String(96), primary_key=True),
        sa.Column("interpretation_json", sa.JSON(), nullable=False),
        sa.Column("interpretation_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    # No inferred manifests, interpretation backfills or execution of old drafts.


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")):
        raise RuntimeError("Request interpretations exist; downgrade would discard evidence")
    op.drop_table(_TABLE)

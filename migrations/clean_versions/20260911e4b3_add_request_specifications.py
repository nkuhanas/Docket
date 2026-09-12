"""record immutable request interpretation proposals

Revision ID: 20260911e4b3
Revises: 20260911d3a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911e4b3"
down_revision: str | None = "20260911d3a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "semantic_request_specifications"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("semantic_request_ref", sa.String(40), sa.ForeignKey(
            "semantic_requests.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("change_set_revision_id", sa.Uuid(), sa.ForeignKey(
            "change_set_revisions.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("interpretation_state", sa.String(40), nullable=False),
        sa.Column("specification_json", sa.JSON(), nullable=False),
        sa.Column("specification_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_request_specifications_version"),
        sa.CheckConstraint("schema_version = 1", name="ck_request_specifications_schema"),
        sa.CheckConstraint("interpretation_state = 'pending_evidence_validation'",
                           name="ck_request_specifications_interpretation"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    # Do not reinterpret existing requests or reconstruct their evidence.


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")):
        raise RuntimeError("Request specifications exist; downgrade would discard evidence")
    op.drop_table(_TABLE)

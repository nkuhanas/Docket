"""add authenticated action roots and optional conversation records

Revision ID: 20261007a1b2
Revises: 20260922d0b9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007a1b2"
down_revision: str | None = "20260922d0b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authenticated_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("principal_ref", sa.String(255), nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(512), nullable=False),
        sa.Column("conversation_ref", sa.String(512), nullable=False),
        sa.Column(
            "source_utterance_ref",
            sa.String(40),
            sa.ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT"),
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("committed_changeset_ref", sa.String(40), unique=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("principal_ref", "request_key", name="uq_agent_requests_identity"),
        sa.CheckConstraint("role = 'interactive'", name="ck_agent_requests_role"),
        sa.CheckConstraint(
            "state IN ('active', 'cancelled', 'committed')", name="ck_agent_requests_state"
        ),
    )
    op.create_table(
        "conversation_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column(
            "request_ref",
            sa.String(40),
            sa.ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("record_key", sa.String(255), nullable=False),
        sa.Column("record_kind", sa.String(32), nullable=False),
        sa.Column("capture_method", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("ciphertext", sa.LargeBinary()),
        sa.Column("nonce", sa.LargeBinary()),
        sa.Column("encryption_key_ref", sa.String(128)),
        sa.Column("gap_code", sa.String(128)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_ref", "record_key", name="uq_conversation_records_key"),
        sa.CheckConstraint(
            "capture_method IN ('agent_reported', 'direct', 'gap')",
            name="ck_conversation_records_method",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_conversation_records_immutable BEFORE UPDATE OR DELETE ON conversation_records FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
        op.execute("""CREATE FUNCTION docket_guard_authenticated_request() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'AuthenticatedRequest is retained'; END IF;
            IF (to_jsonb(NEW) - 'state' - 'committed_changeset_ref') IS DISTINCT FROM
               (to_jsonb(OLD) - 'state' - 'committed_changeset_ref') OR
               OLD.state IN ('cancelled', 'committed') THEN
                RAISE EXCEPTION 'AuthenticatedRequest attribution is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER trg_authenticated_requests_guard BEFORE UPDATE OR DELETE ON authenticated_requests FOR EACH ROW EXECUTE FUNCTION docket_guard_authenticated_request()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM authenticated_requests)")):
        raise RuntimeError("Authenticated requests exist; downgrade would discard authority")
    op.drop_table("conversation_records")
    op.drop_table("authenticated_requests")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_guard_authenticated_request()")

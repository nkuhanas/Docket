"""retain optional request capture and exact authenticated call correlation

Revision ID: 20261007c3d4
Revises: 20261007b2c3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007c3d4"
down_revision: str | None = "20261007b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("authenticated_requests", sa.Column("admitted_timezone", sa.String(128)))
    with op.batch_alter_table("tool_invocations") as batch:
        batch.add_column(sa.Column("authenticated_request_ref", sa.String(40)))
        batch.add_column(sa.Column("assembly_operation_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_tool_invocations_request",
            "authenticated_requests",
            ["authenticated_request_ref"],
            ["ref_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_tool_invocations_operation",
            "assembly_operations",
            ["assembly_operation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("attachment_evidence_metadata") as batch:
        batch.add_column(sa.Column("authenticated_request_ref", sa.String(40)))
        batch.create_foreign_key(
            "fk_attachment_evidence_request",
            "authenticated_requests",
            ["authenticated_request_ref"],
            ["ref_id"],
            ondelete="RESTRICT",
        )
        batch.alter_column("operator_utterance_ref", existing_type=sa.String(40), nullable=True)
        batch.create_check_constraint(
            "ck_attachment_evidence_authority_root",
            "operator_utterance_ref IS NOT NULL OR authenticated_request_ref IS NOT NULL",
        )
    op.add_column("conversation_records", sa.Column("purged_at", sa.DateTime(timezone=True)))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_conversation_records_immutable ON conversation_records")
        op.execute("""CREATE FUNCTION docket_guard_conversation_record() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Conversation metadata is retained'; END IF;
            IF (to_jsonb(NEW) - ARRAY['ciphertext','nonce','encryption_key_ref','purged_at'])
                IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['ciphertext','nonce','encryption_key_ref','purged_at'])
                OR OLD.purged_at IS NOT NULL OR OLD.ciphertext IS NULL
                OR NEW.ciphertext IS NOT NULL OR NEW.nonce IS NOT NULL
                OR NEW.encryption_key_ref IS NOT NULL OR NEW.purged_at IS NULL THEN
                RAISE EXCEPTION 'Conversation records permit irreversible payload purge only';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER trg_conversation_records_guard
            BEFORE UPDATE OR DELETE ON conversation_records FOR EACH ROW
            EXECUTE FUNCTION docket_guard_conversation_record()""")
        op.execute("""CREATE FUNCTION docket_guard_attachment_request_binding()
            RETURNS trigger AS $$ BEGIN
            IF NEW.authenticated_request_ref IS DISTINCT FROM OLD.authenticated_request_ref THEN
                RAISE EXCEPTION 'Attachment request binding is immutable';
            END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER trg_attachment_request_binding
            BEFORE UPDATE ON attachment_evidence_metadata FOR EACH ROW
            EXECUTE FUNCTION docket_guard_attachment_request_binding()""")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("""SELECT
        EXISTS (SELECT 1 FROM authenticated_requests WHERE admitted_timezone IS NOT NULL) OR
        EXISTS (SELECT 1 FROM tool_invocations WHERE authenticated_request_ref IS NOT NULL) OR
        EXISTS (SELECT 1 FROM attachment_evidence_metadata
                WHERE authenticated_request_ref IS NOT NULL) OR
        EXISTS (SELECT 1 FROM conversation_records WHERE purged_at IS NOT NULL)""")
    ):
        raise RuntimeError("Request capture/call history exists; downgrade would discard evidence")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_attachment_request_binding ON attachment_evidence_metadata")
        op.execute("DROP FUNCTION docket_guard_attachment_request_binding()")
        op.execute("DROP TRIGGER trg_conversation_records_guard ON conversation_records")
        op.execute("DROP FUNCTION docket_guard_conversation_record()")
        op.execute("""CREATE TRIGGER trg_conversation_records_immutable
            BEFORE UPDATE OR DELETE ON conversation_records FOR EACH ROW
            EXECUTE FUNCTION docket_reject_immutable_row()""")
    op.drop_column("conversation_records", "purged_at")
    op.drop_column("authenticated_requests", "admitted_timezone")
    with op.batch_alter_table("attachment_evidence_metadata") as batch:
        batch.drop_constraint("ck_attachment_evidence_authority_root", type_="check")
        batch.alter_column("operator_utterance_ref", existing_type=sa.String(40), nullable=False)
        batch.drop_constraint("fk_attachment_evidence_request", type_="foreignkey")
        batch.drop_column("authenticated_request_ref")
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_constraint("fk_tool_invocations_operation", type_="foreignkey")
        batch.drop_constraint("fk_tool_invocations_request", type_="foreignkey")
        batch.drop_column("assembly_operation_id")
        batch.drop_column("authenticated_request_ref")

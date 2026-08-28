"""Add the phase-one provenance and public-reference foundation.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PUBLIC_REF_TABLES = (
    ("audit_events", "aud", "uq_audit_events_ref_id"),
    ("entities", "ent", "uq_entities_ref_id"),
    ("canonical_events", "evt", "uq_canonical_events_ref_id"),
    ("daily_briefs", "brief", "uq_daily_briefs_ref_id"),
    ("source_items", "src", "uq_source_items_ref_id"),
    ("operations", "op", "uq_operations_ref_id"),
    ("calendar_lanes", "lane", "uq_calendar_lanes_ref_id"),
)


def _add_public_ref(table_name: str, prefix: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch:
        batch.add_column(sa.Column("ref_id", sa.String(length=40)))
    table = sa.table(
        table_name,
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String(length=40)),
    )
    legacy_payload = sa.func.upper(
        sa.func.substr(
            sa.func.replace(sa.cast(table.c.id, sa.String()), "-", ""),
            1,
            26,
        )
    )
    op.execute(table.update().values(ref_id=sa.literal(f"{prefix}_") + legacy_payload))
    with op.batch_alter_table(table_name) as batch:
        batch.alter_column("ref_id", existing_type=sa.String(length=40), nullable=False)
        batch.create_unique_constraint(constraint_name, ["ref_id"])


def _drop_public_ref(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch:
        batch.drop_constraint(constraint_name, type_="unique")
        batch.drop_column("ref_id")


def _create_immutability_triggers() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION docket_reject_immutable_provenance()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name in (
        "operator_utterances",
        "interpreted_statements",
        "statement_relations",
        "decisions",
        "audit_events",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_provenance()
            """
        )
    op.execute(
        """
        CREATE FUNCTION docket_guard_agent_response_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.response_key IS DISTINCT FROM OLD.response_key
               OR NEW.conversation_ref IS DISTINCT FROM OLD.conversation_ref
               OR NEW.intent_session_ref IS DISTINCT FROM OLD.intent_session_ref
               OR NEW.responds_to_utterance_refs IS DISTINCT FROM OLD.responds_to_utterance_refs
               OR NEW.basis_refs IS DISTINCT FROM OLD.basis_refs
               OR NEW.verbatim_text IS DISTINCT FROM OLD.verbatim_text
               OR NEW.model_identifier IS DISTINCT FROM OLD.model_identifier
               OR NEW.context_packet_refs IS DISTINCT FROM OLD.context_packet_refs
               OR NEW.tool_call_refs IS DISTINCT FROM OLD.tool_call_refs
               OR NEW.generation_state IS DISTINCT FROM OLD.generation_state
               OR NEW.generated_at IS DISTINCT FROM OLD.generated_at
               OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
               OR NEW.projection_ref IS DISTINCT FROM OLD.projection_ref THEN
                RAISE EXCEPTION 'agent response semantic fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_responses_semantic_immutable
        BEFORE UPDATE ON agent_responses
        FOR EACH ROW EXECUTE FUNCTION docket_guard_agent_response_semantics()
        """
    )


def upgrade() -> None:
    for table_name, prefix, constraint_name in _PUBLIC_REF_TABLES:
        _add_public_ref(table_name, prefix, constraint_name)

    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("primary_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column("affected_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )
        batch.add_column(
            sa.Column("basis_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )

    op.create_table(
        "operator_utterances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("source_message_ref", sa.String(length=512), nullable=False),
        sa.Column("conversation_ref", sa.String(length=512), nullable=False),
        sa.Column("reply_to_source_ref", sa.String(length=512)),
        sa.Column("said_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verbatim_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("request_key", sa.String(length=512), nullable=False),
        sa.Column("source_record_id", sa.Uuid()),
        sa.CheckConstraint(
            "transport IN ('discord')", name="ck_operator_utterances_transport"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["records.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("request_key"),
        sa.UniqueConstraint(
            "transport",
            "source_message_ref",
            name="uq_operator_utterances_transport_message",
        ),
    )
    op.create_index(
        "ix_operator_utterances_conversation_said",
        "operator_utterances",
        ["conversation_ref", "said_at"],
    )

    op.create_table(
        "agent_responses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("response_key", sa.String(length=512), nullable=False),
        sa.Column("conversation_ref", sa.String(length=512), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40)),
        sa.Column("responds_to_utterance_refs", sa.JSON(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("verbatim_text", sa.Text(), nullable=False),
        sa.Column("model_identifier", sa.String(length=255), nullable=False),
        sa.Column("context_packet_refs", sa.JSON(), nullable=False),
        sa.Column("tool_call_refs", sa.JSON(), nullable=False),
        sa.Column("generation_state", sa.String(length=16), nullable=False),
        sa.Column("delivery_state", sa.String(length=16), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projection_ref", sa.String(length=512), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_error_code", sa.String(length=128)),
        sa.CheckConstraint(
            "generation_state = 'complete'", name="ck_agent_responses_generation"
        ),
        sa.CheckConstraint(
            "delivery_state IN ('pending', 'delivered', 'failed')",
            name="ck_agent_responses_delivery",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("projection_ref"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("response_key"),
    )
    op.create_index(
        "ix_agent_responses_conversation_generated",
        "agent_responses",
        ["conversation_ref", "generated_at"],
    )

    op.create_table(
        "agent_response_projections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("response_id", sa.Uuid(), nullable=False),
        sa.Column("projection_ref", sa.String(length=512), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("destination_ref", sa.String(length=512), nullable=False),
        sa.Column("source_message_ref", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_agent_response_projections_status",
        ),
        sa.ForeignKeyConstraint(["response_id"], ["agent_responses.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("projection_ref"),
        sa.UniqueConstraint("response_id"),
    )

    op.create_table(
        "interpreted_statements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("utterance_id", sa.Uuid(), nullable=False),
        sa.Column("statement_kind", sa.String(length=128), nullable=False),
        sa.Column("subject_refs", sa.JSON(), nullable=False),
        sa.Column("predicate", sa.String(length=255), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("affected_fields", sa.JSON(), nullable=False),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("interpretation_json", sa.JSON(), nullable=False),
        sa.Column("interpreter_version", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["utterance_id"], ["operator_utterances.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_interpreted_statements_utterance",
        "interpreted_statements",
        ["utterance_id", "created_at"],
    )

    op.create_table(
        "statement_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_statement_id", sa.Uuid(), nullable=False),
        sa.Column("target_statement_id", sa.Uuid(), nullable=False),
        sa.Column("relation_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relation_kind IN ('affirms', 'amends', 'supersedes', 'contradicts', "
            "'retracts', 'scopes')",
            name="ck_statement_relations_kind",
        ),
        sa.ForeignKeyConstraint(
            ["source_statement_id"], ["interpreted_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_statement_id"], ["interpreted_statements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_statement_id",
            "target_statement_id",
            "relation_kind",
            name="uq_statement_relations_edge",
        ),
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("decision_kind", sa.String(length=128), nullable=False),
        sa.Column("actor_ref", sa.String(length=255)),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("document_ref", sa.String(length=255)),
        sa.Column("frozen_artifact_hash", sa.String(length=64)),
        sa.Column("authorized_scope", sa.String(length=128)),
        sa.Column("architecture_authority", sa.Boolean(), nullable=False),
        sa.Column("implementation_authority", sa.String(length=128)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_decisions_document_kind", "decisions", ["document_ref", "decision_kind"]
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_contract_version", sa.String(length=128), nullable=False),
        sa.Column("caller_profile", sa.String(length=32), nullable=False),
        sa.Column("actor_ref", sa.String(length=255)),
        sa.Column("utterance_refs", sa.JSON(), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40)),
        sa.Column("case_ref", sa.String(length=40)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("received_argument_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_argument_hash", sa.String(length=64)),
        sa.Column("result_refs", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column("mcp_request_id", sa.String(length=255)),
        sa.Column("trace_id", sa.Uuid()),
        sa.Column("trace_call_id", sa.String(length=255)),
        sa.Column("trace_ordinal", sa.Integer()),
        sa.CheckConstraint(
            "caller_profile IN ('interactive', 'triage')",
            name="ck_tool_invocations_profile",
        ),
        sa.CheckConstraint(
            "status IN ('received', 'rejected_validation', 'rejected_authority', "
            "'rejected_conflict', 'succeeded', 'failed')",
            name="ck_tool_invocations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("trace_id", "trace_call_id", name="uq_tool_invocations_trace_call"),
    )
    op.create_index(
        "ix_tool_invocations_name_started", "tool_invocations", ["tool_name", "started_at"]
    )
    op.create_index(
        "ix_tool_invocations_trace", "tool_invocations", ["trace_id", "trace_ordinal"]
    )

    _create_immutability_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_agent_responses_semantic_immutable ON agent_responses"
        )
        op.execute("DROP FUNCTION IF EXISTS docket_guard_agent_response_semantics()")
        for table_name in (
            "operator_utterances",
            "interpreted_statements",
            "statement_relations",
            "decisions",
            "audit_events",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
        op.execute("DROP FUNCTION IF EXISTS docket_reject_immutable_provenance()")

    op.drop_index("ix_tool_invocations_trace", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_name_started", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_index("ix_decisions_document_kind", table_name="decisions")
    op.drop_table("decisions")
    op.drop_table("statement_relations")
    op.drop_index("ix_interpreted_statements_utterance", table_name="interpreted_statements")
    op.drop_table("interpreted_statements")
    op.drop_table("agent_response_projections")
    op.drop_index("ix_agent_responses_conversation_generated", table_name="agent_responses")
    op.drop_table("agent_responses")
    op.drop_index("ix_operator_utterances_conversation_said", table_name="operator_utterances")
    op.drop_table("operator_utterances")

    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("basis_refs")
        batch.drop_column("affected_refs")
        batch.drop_column("primary_ref")

    for table_name, _prefix, constraint_name in reversed(_PUBLIC_REF_TABLES):
        _drop_public_ref(table_name, constraint_name)

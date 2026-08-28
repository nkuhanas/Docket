"""Add durable intent, ChangeSet, and Conflict authority state.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.TextClause:
    return sa.text("'[]'")


def _json_object() -> sa.TextClause:
    return sa.text("'{}'")


def _create_postgres_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in ("change_set_revisions",):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_provenance()
            """
        )
    op.execute(
        """
        CREATE FUNCTION docket_guard_intent_turn_enrichment()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.response_disposition <> 'pending' THEN
                RAISE EXCEPTION 'finalized IntentTurn is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.intent_session_id IS DISTINCT FROM OLD.intent_session_id
               OR NEW.intent_session_ref IS DISTINCT FROM OLD.intent_session_ref
               OR NEW.utterance_ref IS DISTINCT FROM OLD.utterance_ref
               OR NEW.statement_refs IS DISTINCT FROM OLD.statement_refs
               OR NEW.context_refs IS DISTINCT FROM OLD.context_refs
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IntentTurn semantic source fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_intent_turns_enrichment
        BEFORE UPDATE ON intent_turns
        FOR EACH ROW EXECUTE FUNCTION docket_guard_intent_turn_enrichment()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_intent_turns_delete_immutable
        BEFORE DELETE ON intent_turns
        FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_provenance()
        """
    )
    op.execute(
        """
        CREATE FUNCTION docket_guard_committed_changeset()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.state = 'committed' THEN
                RAISE EXCEPTION 'committed ChangeSet is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_change_sets_committed_immutable
        BEFORE UPDATE OR DELETE ON change_sets
        FOR EACH ROW EXECUTE FUNCTION docket_guard_committed_changeset()
        """
    )
    op.execute(
        """
        CREATE FUNCTION docket_guard_conflict_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.subject_refs IS DISTINCT FROM OLD.subject_refs
               OR NEW.affected_fields IS DISTINCT FROM OLD.affected_fields
               OR NEW.prior_statement_refs IS DISTINCT FROM OLD.prior_statement_refs
               OR NEW.incoming_statement_refs IS DISTINCT FROM OLD.incoming_statement_refs
               OR NEW.conflicting_effects_json IS DISTINCT FROM OLD.conflicting_effects_json
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'conflict semantic fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_conflicts_semantic_immutable
        BEFORE UPDATE ON conflicts
        FOR EACH ROW EXECUTE FUNCTION docket_guard_conflict_semantics()
        """
    )


def upgrade() -> None:
    op.add_column(
        "tool_invocations",
        sa.Column(
            "tool_contract_hash",
            sa.String(length=64),
            server_default="0" * 64,
            nullable=False,
        ),
    )
    op.add_column(
        "discord_mcp_traces",
        sa.Column(
            "tool_contract_version",
            sa.String(length=128),
            server_default="pre-contract-bootstrap-2026-08-27",
            nullable=False,
        ),
    )
    op.add_column(
        "discord_mcp_traces",
        sa.Column(
            "tool_contract_hash",
            sa.String(length=64),
            server_default="0" * 64,
            nullable=False,
        ),
    )
    op.add_column(
        "discord_mcp_traces",
        sa.Column(
            "caller_profile",
            sa.String(length=32),
            server_default="interactive",
            nullable=False,
        ),
    )
    op.create_table(
        "intent_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("conversation_ref", sa.String(length=512), nullable=False),
        sa.Column("source_utterance_ref", sa.String(length=40), nullable=False),
        sa.Column("case_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("case_revision_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("brief_ref", sa.String(length=40)),
        sa.Column("trusted_context_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("resolved_intent_json", sa.JSON(), server_default=_json_object(), nullable=False),
        sa.Column(
            "blocking_clarifications",
            sa.JSON(),
            server_default=_json_list(),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("committed_changeset_ref", sa.String(length=40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('open', 'needs_clarification', 'ready', 'committed', "
            "'cancelled', 'superseded')",
            name="ck_intent_sessions_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_intent_sessions_conversation_state",
        "intent_sessions",
        ["conversation_ref", "state"],
    )
    op.create_index(
        "ix_intent_sessions_source_utterance",
        "intent_sessions",
        ["source_utterance_ref"],
    )

    op.create_table(
        "intent_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("intent_session_id", sa.Uuid(), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40), nullable=False),
        sa.Column("utterance_ref", sa.String(length=40), nullable=False),
        sa.Column("statement_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("context_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("tool_call_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("agent_response_ref", sa.String(length=40)),
        sa.Column(
            "resulting_semantic_refs", sa.JSON(), server_default=_json_list(), nullable=False
        ),
        sa.Column("response_disposition", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "response_disposition IN ('pending', 'final_response', 'no_response')",
            name="ck_intent_turns_response_disposition",
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_id"], ["intent_sessions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_ref"], ["intent_sessions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "intent_session_id", "utterance_ref", name="uq_intent_turns_session_utterance"
        ),
    )
    op.create_index(
        "ix_intent_turns_session_created",
        "intent_turns",
        ["intent_session_id", "created_at"],
    )

    op.create_table(
        "change_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("intent_session_id", sa.Uuid(), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("expected_versions", sa.JSON(), server_default=_json_object(), nullable=False),
        sa.Column("registry_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("preference_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("lane_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("event_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("resolution_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("provider_intents", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("validation_errors", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('draft', 'validated', 'committed', 'invalidated', "
            "'cancelled', 'superseded')",
            name="ck_change_sets_state",
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_id"], ["intent_sessions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_ref"], ["intent_sessions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_change_sets_session_state", "change_sets", ["intent_session_id", "state"]
    )

    op.create_table(
        "change_set_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("expected_versions", sa.JSON(), server_default=_json_object(), nullable=False),
        sa.Column("registry_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("preference_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("lane_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("event_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("resolution_changes", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("provider_intents", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("parameter_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "change_set_id", "revision", name="uq_change_set_revisions_number"
        ),
    )
    op.create_index(
        "ix_change_set_revisions_changeset",
        "change_set_revisions",
        ["change_set_id", "revision"],
    )

    op.create_table(
        "conflicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("subject_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("affected_fields", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("prior_statement_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column(
            "incoming_statement_refs", sa.JSON(), server_default=_json_list(), nullable=False
        ),
        sa.Column(
            "conflicting_effects_json", sa.JSON(), server_default=_json_object(), nullable=False
        ),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("resolution_decision_ref", sa.String(length=40)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'resolved_supersession', "
            "'resolved_scoped_coexistence', 'resolved_retraction', 'cancelled')",
            name="ck_conflicts_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index("ix_conflicts_status_created", "conflicts", ["status", "created_at"])

    with op.batch_alter_table("operations") as batch:
        batch.add_column(sa.Column("originating_changeset_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column(
                "canonical_target_refs", sa.JSON(), server_default=_json_list(), nullable=False
            )
        )
    op.create_index(
        "ix_operations_originating_changeset_ref",
        "operations",
        ["originating_changeset_ref"],
    )

    _create_postgres_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_conflicts_semantic_immutable ON conflicts")
        op.execute("DROP FUNCTION docket_guard_conflict_semantics()")
        op.execute("DROP TRIGGER trg_change_sets_committed_immutable ON change_sets")
        op.execute("DROP FUNCTION docket_guard_committed_changeset()")
        op.execute("DROP TRIGGER trg_intent_turns_delete_immutable ON intent_turns")
        op.execute("DROP TRIGGER trg_intent_turns_enrichment ON intent_turns")
        op.execute("DROP FUNCTION docket_guard_intent_turn_enrichment()")
        op.execute(
            "DROP TRIGGER trg_change_set_revisions_immutable ON change_set_revisions"
        )

    op.drop_index("ix_operations_originating_changeset_ref", table_name="operations")
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("canonical_target_refs")
        batch.drop_column("basis_refs")
        batch.drop_column("originating_changeset_ref")

    op.drop_index("ix_conflicts_status_created", table_name="conflicts")
    op.drop_table("conflicts")
    op.drop_index("ix_change_set_revisions_changeset", table_name="change_set_revisions")
    op.drop_table("change_set_revisions")
    op.drop_index("ix_change_sets_session_state", table_name="change_sets")
    op.drop_table("change_sets")
    op.drop_index("ix_intent_turns_session_created", table_name="intent_turns")
    op.drop_table("intent_turns")
    op.drop_index("ix_intent_sessions_source_utterance", table_name="intent_sessions")
    op.drop_index("ix_intent_sessions_conversation_state", table_name="intent_sessions")
    op.drop_table("intent_sessions")
    op.drop_column("discord_mcp_traces", "caller_profile")
    op.drop_column("discord_mcp_traces", "tool_contract_hash")
    op.drop_column("discord_mcp_traces", "tool_contract_version")
    op.drop_column("tool_invocations", "tool_contract_hash")

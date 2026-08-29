"""Add durable interactive-authority continuity primitives.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _new_ref(prefix: str) -> str:
    value = (int(time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return prefix + "_" + "".join(encoded)


def _replace_postgres_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION docket_guard_agent_response_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.response_key IS DISTINCT FROM OLD.response_key
               OR NEW.conversation_ref IS DISTINCT FROM OLD.conversation_ref
               OR NEW.intent_session_ref IS DISTINCT FROM OLD.intent_session_ref
               OR NEW.responds_to_utterance_refs::jsonb
                  IS DISTINCT FROM OLD.responds_to_utterance_refs::jsonb
               OR NEW.basis_refs::jsonb IS DISTINCT FROM OLD.basis_refs::jsonb
               OR NEW.verbatim_text IS DISTINCT FROM OLD.verbatim_text
               OR NEW.model_identifier IS DISTINCT FROM OLD.model_identifier
               OR NEW.context_packet_refs::jsonb
                  IS DISTINCT FROM OLD.context_packet_refs::jsonb
               OR NEW.tool_call_refs::jsonb IS DISTINCT FROM OLD.tool_call_refs::jsonb
               OR NEW.generation_state IS DISTINCT FROM OLD.generation_state
               OR NEW.generated_at IS DISTINCT FROM OLD.generated_at
               OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
               OR NEW.projection_ref IS DISTINCT FROM OLD.projection_ref
               OR NEW.gateway_instance_ref IS DISTINCT FROM OLD.gateway_instance_ref THEN
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
        CREATE OR REPLACE FUNCTION docket_guard_intent_turn_enrichment()
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
               OR NEW.statement_refs::jsonb IS DISTINCT FROM OLD.statement_refs::jsonb
               OR NEW.context_refs::jsonb IS DISTINCT FROM OLD.context_refs::jsonb
               OR NEW.semantic_request_ref IS DISTINCT FROM OLD.semantic_request_ref
               OR NEW.authority_substitutions_json::jsonb
                  IS DISTINCT FROM OLD.authority_substitutions_json::jsonb
               OR NEW.gateway_instance_ref IS DISTINCT FROM OLD.gateway_instance_ref
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
        CREATE TRIGGER trg_persisted_semantic_options_immutable
        BEFORE UPDATE OR DELETE ON persisted_semantic_options
        FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_provenance()
        """
    )


def _restore_postgres_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION docket_guard_agent_response_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.response_key IS DISTINCT FROM OLD.response_key
               OR NEW.conversation_ref IS DISTINCT FROM OLD.conversation_ref
               OR NEW.intent_session_ref IS DISTINCT FROM OLD.intent_session_ref
               OR NEW.responds_to_utterance_refs::jsonb
                  IS DISTINCT FROM OLD.responds_to_utterance_refs::jsonb
               OR NEW.basis_refs::jsonb IS DISTINCT FROM OLD.basis_refs::jsonb
               OR NEW.verbatim_text IS DISTINCT FROM OLD.verbatim_text
               OR NEW.model_identifier IS DISTINCT FROM OLD.model_identifier
               OR NEW.context_packet_refs::jsonb
                  IS DISTINCT FROM OLD.context_packet_refs::jsonb
               OR NEW.tool_call_refs::jsonb IS DISTINCT FROM OLD.tool_call_refs::jsonb
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
        CREATE OR REPLACE FUNCTION docket_guard_intent_turn_enrichment()
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
               OR NEW.statement_refs::jsonb IS DISTINCT FROM OLD.statement_refs::jsonb
               OR NEW.context_refs::jsonb IS DISTINCT FROM OLD.context_refs::jsonb
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IntentTurn semantic source fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def upgrade() -> None:
    with op.batch_alter_table("discord_projections") as batch:
        batch.add_column(sa.Column("ref_id", sa.String(length=40)))
    projections = sa.table(
        "discord_projections",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String(length=40)),
    )
    bind = op.get_bind()
    for projection_id in bind.scalars(sa.select(projections.c.id)):
        bind.execute(
            projections.update()
            .where(projections.c.id == projection_id)
            .values(ref_id=_new_ref("proj"))
        )
    with op.batch_alter_table("discord_projections") as batch:
        batch.alter_column("ref_id", existing_type=sa.String(length=40), nullable=False)
        batch.create_unique_constraint("uq_discord_projections_ref_id", ["ref_id"])

    with op.batch_alter_table("operator_utterances") as batch:
        batch.add_column(
            sa.Column(
                "utterance_kind",
                sa.String(length=32),
                server_default="typed_message",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("selected_option_id", sa.String(length=128)))
        batch.add_column(sa.Column("visible_choice_text", sa.Text()))
        batch.add_column(sa.Column("authority_scope_hash", sa.String(length=64)))
        batch.add_column(sa.Column("selected_precondition_hash", sa.String(length=64)))
        batch.add_column(sa.Column("prompt_projection_ref", sa.String(length=40)))
        batch.add_column(sa.Column("prompt_projection_version", sa.Integer()))
        batch.add_column(sa.Column("case_ref", sa.String(length=40)))
        batch.add_column(sa.Column("case_revision_ref", sa.String(length=40)))
        batch.add_column(sa.Column("intent_session_ref", sa.String(length=40)))
        batch.add_column(sa.Column("discord_interaction_ref", sa.String(length=255)))
        batch.create_unique_constraint(
            "uq_operator_utterances_discord_interaction_ref",
            ["discord_interaction_ref"],
        )
        batch.create_check_constraint(
            "ck_operator_utterances_kind",
            "utterance_kind IN ('typed_message', 'button_selection', "
            "'select_selection', 'modal_submission')",
        )
        batch.create_check_constraint(
            "ck_operator_utterances_selection_binding",
            "(utterance_kind NOT IN ('button_selection', 'select_selection')) OR "
            "(selected_option_id IS NOT NULL AND visible_choice_text IS NOT NULL AND "
            "authority_scope_hash IS NOT NULL AND selected_precondition_hash IS NOT NULL "
            "AND prompt_projection_ref IS NOT NULL AND "
            "prompt_projection_version IS NOT NULL AND intent_session_ref IS NOT NULL "
            "AND discord_interaction_ref IS NOT NULL)",
        )

    with op.batch_alter_table("intent_sessions") as batch:
        batch.add_column(
            sa.Column(
                "semantic_state",
                sa.String(length=32),
                server_default="open",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "commit_state",
                sa.String(length=32),
                server_default="not_attempted",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("semantic_request_ref", sa.String(length=40)))

    sessions = sa.table(
        "intent_sessions",
        sa.column("state", sa.String(length=32)),
        sa.column("semantic_state", sa.String(length=32)),
        sa.column("commit_state", sa.String(length=32)),
    )
    bind.execute(
        sessions.update().values(
            semantic_state=sa.case(
                (sessions.c.state == "committed", "ready"),
                else_=sessions.c.state,
            ),
            commit_state=sa.case(
                (sessions.c.state == "committed", "committed"),
                else_="not_attempted",
            ),
        )
    )
    with op.batch_alter_table("intent_sessions") as batch:
        batch.create_check_constraint(
            "ck_intent_sessions_semantic_state",
            "semantic_state IN ('open', 'needs_clarification', 'ready', "
            "'cancelled', 'superseded')",
        )
        batch.create_check_constraint(
            "ck_intent_sessions_commit_state",
            "commit_state IN ('not_attempted', 'pending', 'committed', "
            "'blocked_validation', 'blocked_conflict', 'blocked_version', "
            "'failed', 'unknown')",
        )
        batch.create_index(
            "ix_intent_sessions_semantic_commit", ["semantic_state", "commit_state"]
        )

    with op.batch_alter_table("intent_turns") as batch:
        batch.add_column(sa.Column("semantic_request_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column(
                "authority_substitutions_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("gateway_instance_ref", sa.String(length=40)))

    for table_name in ("change_sets", "change_set_revisions"):
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(sa.Column("semantic_request_ref", sa.String(length=40)))
            batch.add_column(sa.Column("authority_scope_hash", sa.String(length=64)))
            batch.add_column(sa.Column("precondition_hash", sa.String(length=64)))
            batch.add_column(
                sa.Column(
                    "execution_binding_json",
                    sa.JSON(),
                    server_default=sa.text("'{}'"),
                    nullable=False,
                )
            )
    with op.batch_alter_table("change_sets") as batch:
        batch.create_unique_constraint(
            "uq_change_sets_semantic_request", ["semantic_request_ref"]
        )

    with op.batch_alter_table("agent_responses") as batch:
        batch.add_column(sa.Column("gateway_instance_ref", sa.String(length=40)))

    with op.batch_alter_table("tool_invocations") as batch:
        batch.add_column(
            sa.Column(
                "transport_state",
                sa.String(length=16),
                server_default="running",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "domain_state",
                sa.String(length=16),
                server_default="unknown",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("semantic_request_ref", sa.String(length=40)))
        batch.add_column(sa.Column("gateway_instance_ref", sa.String(length=40)))
    invocations = sa.table(
        "tool_invocations",
        sa.column("status", sa.String(length=32)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
        sa.column("transport_state", sa.String(length=16)),
        sa.column("domain_state", sa.String(length=16)),
    )
    bind.execute(
        invocations.update().values(
            transport_state=sa.case(
                (invocations.c.completed_at.is_not(None), "completed"),
                else_="running",
            ),
            domain_state=sa.case(
                (invocations.c.status == "succeeded", "succeeded"),
                (invocations.c.status.like("rejected_%"), "rejected"),
                (invocations.c.status == "failed", "failed"),
                else_="unknown",
            ),
        )
    )
    with op.batch_alter_table("tool_invocations") as batch:
        batch.create_check_constraint(
            "ck_tool_invocations_transport_state",
            "transport_state IN ('running', 'completed', 'failed', 'timed_out')",
        )
        batch.create_check_constraint(
            "ck_tool_invocations_domain_state",
            "domain_state IN ('succeeded', 'rejected', 'failed', 'unknown')",
        )

    with op.batch_alter_table("discord_mcp_traces") as batch:
        batch.add_column(sa.Column("gateway_instance_ref", sa.String(length=40)))

    op.create_table(
        "semantic_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("intent_session_id", sa.Uuid(), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40), nullable=False),
        sa.Column("authority_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("current_precondition_hash", sa.String(length=64), nullable=False),
        sa.Column("origin_utterance_refs", sa.JSON(), nullable=False),
        sa.Column("selected_option_binding", sa.JSON()),
        sa.Column("authority_availability", sa.String(length=32), nullable=False),
        sa.Column("commit_state", sa.String(length=32), nullable=False),
        sa.Column("current_case_revision_ref", sa.String(length=40)),
        sa.Column("symbolic_substitutions_json", sa.JSON(), nullable=False),
        sa.Column("committed_changeset_ref", sa.String(length=40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "authority_availability IN ('available', 'consumed_committed', "
            "'cancelled', 'superseded', 'invalidated_by_state')",
            name="ck_semantic_requests_authority_availability",
        ),
        sa.CheckConstraint(
            "commit_state IN ('not_attempted', 'pending', 'committed', "
            "'blocked_validation', 'blocked_conflict', 'blocked_version', "
            "'failed', 'unknown')",
            name="ck_semantic_requests_commit_state",
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
            "intent_session_ref",
            "authority_scope_hash",
            name="uq_semantic_requests_session_scope",
        ),
        sa.UniqueConstraint(
            "committed_changeset_ref",
            name="uq_semantic_requests_committed_changeset",
        ),
    )
    op.create_index(
        "ix_semantic_requests_session_state",
        "semantic_requests",
        ["intent_session_ref", "commit_state"],
    )

    op.create_table(
        "semantic_request_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("semantic_request_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_request_ref", sa.String(length=40), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("authority_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("precondition_hash", sa.String(length=64), nullable=False),
        sa.Column("case_revision_ref", sa.String(length=40)),
        sa.Column("change_set_ref", sa.String(length=40)),
        sa.Column("tool_call_ref", sa.String(length=40)),
        sa.Column("gateway_instance_ref", sa.String(length=40)),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column("error_details_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('pending', 'committed', 'blocked_validation', "
            "'blocked_conflict', 'blocked_version', 'failed', 'unknown')",
            name="ck_semantic_request_attempts_state",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_request_id"], ["semantic_requests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["semantic_request_ref"], ["semantic_requests.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "semantic_request_id",
            "attempt_number",
            name="uq_semantic_request_attempts_number",
        ),
        sa.UniqueConstraint("tool_call_ref", name="uq_semantic_request_attempts_call"),
    )
    op.create_index(
        "ix_semantic_request_attempts_request",
        "semantic_request_attempts",
        ["semantic_request_id", "attempt_number"],
    )

    op.create_table(
        "semantic_prompt_projections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("intent_session_ref", sa.String(length=40), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("parent_channel_id", sa.String(length=64)),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("case_ref", sa.String(length=40)),
        sa.Column("case_revision_ref", sa.String(length=40)),
        sa.Column("render_sha256", sa.String(length=64), nullable=False),
        sa.Column("component_sha256", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=64)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'selected', 'superseded')",
            name="ck_semantic_prompt_projections_status",
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_ref"], ["intent_sessions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("message_id", name="uq_semantic_prompt_projections_message"),
        sa.UniqueConstraint(
            "intent_session_ref",
            "projection_version",
            name="uq_semantic_prompt_projections_session_version",
        ),
    )
    op.create_index(
        "ix_semantic_prompt_projections_status_created",
        "semantic_prompt_projections",
        ["status", "created_at"],
    )

    op.create_table(
        "persisted_semantic_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("prompt_projection_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_projection_ref", sa.String(length=40), nullable=False),
        sa.Column("prompt_projection_version", sa.Integer(), nullable=False),
        sa.Column("option_id", sa.String(length=128), nullable=False),
        sa.Column("visible_text", sa.Text(), nullable=False),
        sa.Column("action_kind", sa.String(length=128), nullable=False),
        sa.Column("authority_scope_json", sa.JSON(), nullable=False),
        sa.Column("execution_preconditions_json", sa.JSON(), nullable=False),
        sa.Column("compilation_template_json", sa.JSON(), nullable=False),
        sa.Column("case_ref", sa.String(length=40)),
        sa.Column("case_revision_ref", sa.String(length=40)),
        sa.Column("intent_session_ref", sa.String(length=40), nullable=False),
        sa.Column("authority_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("precondition_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["prompt_projection_id"], ["semantic_prompt_projections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_projection_ref"], ["semantic_prompt_projections.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["intent_session_ref"], ["intent_sessions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "prompt_projection_ref",
            "prompt_projection_version",
            "option_id",
            name="uq_persisted_semantic_options_binding",
        ),
        sa.UniqueConstraint(
            "prompt_projection_ref",
            "prompt_projection_version",
            "option_id",
            "authority_scope_hash",
            "precondition_hash",
            name="uq_persisted_semantic_options_exact_binding",
        ),
    )
    op.create_index(
        "ix_persisted_semantic_options_session",
        "persisted_semantic_options",
        ["intent_session_ref", "created_at"],
    )

    op.create_table(
        "gateway_lifetimes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("instance_kind", sa.String(length=64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clean_shutdown_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'draining', 'clean_shutdown', 'expired')",
            name="ck_gateway_lifetimes_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "instance_kind", "lease_generation", name="uq_gateway_lifetimes_generation"
        ),
    )
    op.create_index(
        "ix_gateway_lifetimes_status_expiry",
        "gateway_lifetimes",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "drain_barriers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.CheckConstraint(
            "status IN ('requested', 'draining', 'released', 'aborted')",
            name="ck_drain_barriers_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_drain_barriers_status_requested",
        "drain_barriers",
        ["status", "requested_at"],
    )

    op.create_table(
        "execution_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("lease_key", sa.String(length=512), nullable=False),
        sa.Column("lease_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_ref", sa.String(length=40)),
        sa.Column("gateway_instance_ref", sa.String(length=40)),
        sa.Column("claimed_before_drain_ref", sa.String(length=40)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "lease_kind IN ('interactive_turn', 'triage_turn', 'tool_invocation', "
            "'provider_call', 'outbox_delivery', 'cron_execution')",
            name="ck_execution_leases_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'expired', 'cancelled')",
            name="ck_execution_leases_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("lease_key", name="uq_execution_leases_key"),
    )
    op.create_index(
        "ix_execution_leases_status_expiry",
        "execution_leases",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "deferred_ingress",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("ingress_kind", sa.String(length=32), nullable=False),
        sa.Column("utterance_ref", sa.String(length=40), nullable=False),
        sa.Column("selected_option_binding_json", sa.JSON()),
        sa.Column("drain_ref", sa.String(length=40)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_by_gateway_ref", sa.String(length=40)),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.CheckConstraint(
            "ingress_kind IN ('typed_message', 'button_selection', "
            "'select_selection', 'modal_submission')",
            name="ck_deferred_ingress_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'completed', 'rejected')",
            name="ck_deferred_ingress_status",
        ),
        sa.ForeignKeyConstraint(
            ["utterance_ref"], ["operator_utterances.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("source_key", name="uq_deferred_ingress_source_key"),
        sa.UniqueConstraint("utterance_ref", name="uq_deferred_ingress_utterance"),
    )
    op.create_index(
        "ix_deferred_ingress_status_created",
        "deferred_ingress",
        ["status", "created_at"],
    )
    _replace_postgres_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_persisted_semantic_options_immutable "
            "ON persisted_semantic_options"
        )
    op.drop_index("ix_deferred_ingress_status_created", table_name="deferred_ingress")
    op.drop_table("deferred_ingress")
    op.drop_index("ix_execution_leases_status_expiry", table_name="execution_leases")
    op.drop_table("execution_leases")
    op.drop_index("ix_drain_barriers_status_requested", table_name="drain_barriers")
    op.drop_table("drain_barriers")
    op.drop_index("ix_gateway_lifetimes_status_expiry", table_name="gateway_lifetimes")
    op.drop_table("gateway_lifetimes")
    op.drop_index(
        "ix_persisted_semantic_options_session",
        table_name="persisted_semantic_options",
    )
    op.drop_table("persisted_semantic_options")
    op.drop_index(
        "ix_semantic_prompt_projections_status_created",
        table_name="semantic_prompt_projections",
    )
    op.drop_table("semantic_prompt_projections")
    op.drop_index("ix_semantic_request_attempts_request", table_name="semantic_request_attempts")
    op.drop_table("semantic_request_attempts")
    op.drop_index("ix_semantic_requests_session_state", table_name="semantic_requests")
    op.drop_table("semantic_requests")

    with op.batch_alter_table("discord_mcp_traces") as batch:
        batch.drop_column("gateway_instance_ref")
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_constraint("ck_tool_invocations_domain_state", type_="check")
        batch.drop_constraint("ck_tool_invocations_transport_state", type_="check")
        batch.drop_column("gateway_instance_ref")
        batch.drop_column("semantic_request_ref")
        batch.drop_column("domain_state")
        batch.drop_column("transport_state")
    with op.batch_alter_table("agent_responses") as batch:
        batch.drop_column("gateway_instance_ref")
    with op.batch_alter_table("change_sets") as batch:
        batch.drop_constraint("uq_change_sets_semantic_request", type_="unique")
    for table_name in ("change_set_revisions", "change_sets"):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_column("execution_binding_json")
            batch.drop_column("precondition_hash")
            batch.drop_column("authority_scope_hash")
            batch.drop_column("semantic_request_ref")
    with op.batch_alter_table("intent_turns") as batch:
        batch.drop_column("gateway_instance_ref")
        batch.drop_column("authority_substitutions_json")
        batch.drop_column("semantic_request_ref")
    with op.batch_alter_table("intent_sessions") as batch:
        batch.drop_index("ix_intent_sessions_semantic_commit")
        batch.drop_constraint("ck_intent_sessions_commit_state", type_="check")
        batch.drop_constraint("ck_intent_sessions_semantic_state", type_="check")
        batch.drop_column("semantic_request_ref")
        batch.drop_column("commit_state")
        batch.drop_column("semantic_state")
    with op.batch_alter_table("operator_utterances") as batch:
        batch.drop_constraint("ck_operator_utterances_selection_binding", type_="check")
        batch.drop_constraint("ck_operator_utterances_kind", type_="check")
        batch.drop_constraint(
            "uq_operator_utterances_discord_interaction_ref", type_="unique"
        )
        batch.drop_column("discord_interaction_ref")
        batch.drop_column("intent_session_ref")
        batch.drop_column("case_revision_ref")
        batch.drop_column("case_ref")
        batch.drop_column("prompt_projection_version")
        batch.drop_column("prompt_projection_ref")
        batch.drop_column("selected_precondition_hash")
        batch.drop_column("authority_scope_hash")
        batch.drop_column("visible_choice_text")
        batch.drop_column("selected_option_id")
        batch.drop_column("utterance_kind")
    with op.batch_alter_table("discord_projections") as batch:
        batch.drop_constraint("uq_discord_projections_ref_id", type_="unique")
        batch.drop_column("ref_id")
    _restore_postgres_guards()

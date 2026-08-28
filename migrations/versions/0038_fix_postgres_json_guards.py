"""Make PostgreSQL provenance guards compare JSON values through JSONB.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_guards(*, cast_jsonb: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    cast = "::jsonb" if cast_jsonb else ""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION docket_guard_agent_response_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.response_key IS DISTINCT FROM OLD.response_key
               OR NEW.conversation_ref IS DISTINCT FROM OLD.conversation_ref
               OR NEW.intent_session_ref IS DISTINCT FROM OLD.intent_session_ref
               OR NEW.responds_to_utterance_refs{cast}
                  IS DISTINCT FROM OLD.responds_to_utterance_refs{cast}
               OR NEW.basis_refs{cast} IS DISTINCT FROM OLD.basis_refs{cast}
               OR NEW.verbatim_text IS DISTINCT FROM OLD.verbatim_text
               OR NEW.model_identifier IS DISTINCT FROM OLD.model_identifier
               OR NEW.context_packet_refs{cast}
                  IS DISTINCT FROM OLD.context_packet_refs{cast}
               OR NEW.tool_call_refs{cast} IS DISTINCT FROM OLD.tool_call_refs{cast}
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
        f"""
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
               OR NEW.statement_refs{cast} IS DISTINCT FROM OLD.statement_refs{cast}
               OR NEW.context_refs{cast} IS DISTINCT FROM OLD.context_refs{cast}
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
        f"""
        CREATE OR REPLACE FUNCTION docket_guard_conflict_semantics()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ref_id IS DISTINCT FROM OLD.ref_id
               OR NEW.subject_refs{cast} IS DISTINCT FROM OLD.subject_refs{cast}
               OR NEW.affected_fields{cast} IS DISTINCT FROM OLD.affected_fields{cast}
               OR NEW.prior_statement_refs{cast}
                  IS DISTINCT FROM OLD.prior_statement_refs{cast}
               OR NEW.incoming_statement_refs{cast}
                  IS DISTINCT FROM OLD.incoming_statement_refs{cast}
               OR NEW.conflicting_effects_json{cast}
                  IS DISTINCT FROM OLD.conflicting_effects_json{cast}
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'conflict semantic fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def upgrade() -> None:
    _replace_guards(cast_jsonb=True)


def downgrade() -> None:
    _replace_guards(cast_jsonb=False)

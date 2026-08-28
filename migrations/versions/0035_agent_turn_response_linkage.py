"""Complete AgentResponse projection and IntentTurn linkage metadata.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    is_postgresql = op.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_agent_responses_semantic_immutable "
            "ON agent_responses"
        )
    with op.batch_alter_table("agent_response_projections") as batch:
        batch.add_column(sa.Column("operator_ref", sa.String(length=255)))
        batch.add_column(sa.Column("primary_public_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column("projection_version", sa.Integer(), server_default="1", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "case_revision_refs",
                sa.JSON(),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("brief_ref", sa.String(length=40)))

    bind = op.get_bind()
    metadata = sa.MetaData()
    projections = sa.Table("agent_response_projections", metadata, autoload_with=bind)
    responses = sa.Table("agent_responses", metadata, autoload_with=bind)
    utterances = sa.Table("operator_utterances", metadata, autoload_with=bind)
    sessions = sa.Table("intent_sessions", metadata, autoload_with=bind)
    turns = sa.Table("intent_turns", metadata, autoload_with=bind)
    invocations = sa.Table("tool_invocations", metadata, autoload_with=bind)
    for row in bind.execute(
        sa.select(
            projections.c.id,
            responses.c.ref_id,
            responses.c.responds_to_utterance_refs,
            responses.c.intent_session_ref,
            responses.c.context_packet_refs,
            responses.c.tool_call_refs,
        ).join(responses, responses.c.id == projections.c.response_id)
    ):
        utterance_refs = list(row.responds_to_utterance_refs or [])
        if len(utterance_refs) != 1:
            raise RuntimeError("AgentResponse migration requires exactly one source utterance")
        actor_ref = bind.scalar(
            sa.select(utterances.c.actor_ref).where(
                utterances.c.ref_id == utterance_refs[0]
            )
        )
        if actor_ref is None:
            raise RuntimeError("AgentResponse migration could not resolve its Operator")
        case_revision_refs: list[str] = []
        brief_ref: str | None = None
        intent_session_ref = row.intent_session_ref
        context_packet_refs = list(row.context_packet_refs or [])
        matching_turns = bind.execute(
            sa.select(
                turns.c.id,
                turns.c.ref_id,
                turns.c.intent_session_ref,
                turns.c.context_refs,
                turns.c.response_disposition,
            ).where(turns.c.utterance_ref == utterance_refs[0])
        ).all()
        if len(matching_turns) > 1:
            raise RuntimeError("OperatorUtterance migration resolves to multiple IntentTurns")
        if matching_turns:
            turn = matching_turns[0]
            intent_session_ref = turn.intent_session_ref
            context_packet_refs = [
                ref for ref in list(turn.context_refs or []) if ref.startswith("ctx_")
            ]
            semantic_refs: list[str] = []
            if row.tool_call_refs:
                for invocation in bind.execute(
                    sa.select(invocations.c.tool_name, invocations.c.result_refs).where(
                        invocations.c.ref_id.in_(list(row.tool_call_refs))
                    )
                ):
                    if invocation.tool_name not in {
                        "docket_commit_changeset",
                        "docket_resolve_conflict",
                    }:
                        continue
                    for ref in list(invocation.result_refs or []):
                        if ref not in semantic_refs:
                            semantic_refs.append(ref)
            if turn.response_disposition == "pending":
                bind.execute(
                    turns.update()
                    .where(turns.c.id == turn.id)
                    .values(
                        tool_call_refs=list(row.tool_call_refs or []),
                        agent_response_ref=row.ref_id,
                        resulting_semantic_refs=semantic_refs,
                        response_disposition="final_response",
                    )
                )
            bind.execute(
                responses.update()
                .where(responses.c.ref_id == row.ref_id)
                .values(
                    intent_session_ref=intent_session_ref,
                    context_packet_refs=context_packet_refs,
                )
            )
        if intent_session_ref is not None:
            intent = bind.execute(
                sa.select(sessions.c.case_revision_refs, sessions.c.brief_ref).where(
                    sessions.c.ref_id == intent_session_ref
                )
            ).one_or_none()
            if intent is not None:
                case_revision_refs = list(intent.case_revision_refs or [])
                brief_ref = intent.brief_ref
        bind.execute(
            projections.update()
            .where(projections.c.id == row.id)
            .values(
                operator_ref=actor_ref,
                primary_public_ref=row.ref_id,
                case_revision_refs=case_revision_refs,
                brief_ref=brief_ref,
            )
        )

    if is_postgresql:
        op.execute(
            "CREATE TRIGGER trg_agent_responses_semantic_immutable "
            "BEFORE UPDATE ON agent_responses FOR EACH ROW EXECUTE FUNCTION "
            "docket_guard_agent_response_semantics()"
        )

    with op.batch_alter_table("agent_response_projections") as batch:
        batch.alter_column("operator_ref", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column(
            "primary_public_ref", existing_type=sa.String(length=40), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_response_projections") as batch:
        batch.drop_column("brief_ref")
        batch.drop_column("case_revision_refs")
        batch.drop_column("projection_version")
        batch.drop_column("primary_public_ref")
        batch.drop_column("operator_ref")

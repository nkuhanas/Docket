"""bind existing workflow records to authenticated requests without backfill

Revision ID: 20261007b2c3
Revises: 20261007a1b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007b2c3"
down_revision: str | None = "20261007a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = (
    ("intent_sessions", "source_request_ref", "source_utterance_ref", sa.String(40), True),
    ("intent_turns", "request_ref", "utterance_ref", sa.String(40), False),
    ("semantic_requests", "authenticated_request_ref", None, None, True),
    ("assembly_executions", "source_request_ref", "source_utterance_ref", sa.String(40), False),
    ("assembly_operations", "source_request_ref", "source_utterance_ref", sa.String(40), False),
    ("interpreted_statements", "authenticated_request_ref", "utterance_id", sa.Uuid(), False),
)


def upgrade() -> None:
    with op.batch_alter_table("semantic_request_specifications") as batch:
        batch.drop_constraint("ck_request_specifications_schema", type_="check")
        batch.drop_constraint("ck_request_specifications_interpretation", type_="check")
        batch.create_check_constraint(
            "ck_request_specifications_schema", "schema_version IN (1, 2)"
        )
        batch.create_check_constraint(
            "ck_request_specifications_interpretation",
            "(schema_version = 1 AND interpretation_state = 'pending_evidence_validation') OR "
            "(schema_version = 2 AND interpretation_state = 'agent_reported_interpretation')",
        )
    for table, column, old_column, old_type, unique in _LINKS:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.String(40)))
            batch.create_foreign_key(
                f"fk_{table}_{column}",
                "authenticated_requests",
                [column],
                ["ref_id"],
                ondelete="RESTRICT",
            )
            if unique:
                batch.create_unique_constraint(f"uq_{table}_{column}", [column])
            if old_column is not None:
                batch.alter_column(old_column, existing_type=old_type, nullable=True)
                batch.create_check_constraint(
                    f"ck_{table}_authority_root",
                    f"{old_column} IS NOT NULL OR {column} IS NOT NULL",
                )
    with op.batch_alter_table("assembly_executions") as batch:
        batch.add_column(sa.Column("execution_key", sa.String(255)))
        batch.alter_column("trace_ref", existing_type=sa.String(40), nullable=True)
        batch.create_unique_constraint(
            "uq_assembly_agent_execution", ["source_request_ref", "execution_key"]
        )
    with op.batch_alter_table("assembly_operations") as batch:
        batch.alter_column("trace_ref", existing_type=sa.String(40), nullable=True)
    with op.batch_alter_table("intent_turns") as batch:
        batch.create_unique_constraint(
            "uq_intent_turns_session_request", ["intent_session_id", "request_ref"]
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM semantic_request_specifications WHERE schema_version = 2)"
        )
    ):
        raise RuntimeError("Agent interpretation history exists; downgrade would discard evidence")
    for table, column, _old_column, _old_type, _unique in _LINKS:
        if op.get_bind().scalar(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {column} IS NOT NULL)")
        ):
            raise RuntimeError("Request-bound work exists; downgrade would discard authority")
    with op.batch_alter_table("intent_turns") as batch:
        batch.drop_constraint("uq_intent_turns_session_request", type_="unique")
    with op.batch_alter_table("assembly_executions") as batch:
        batch.drop_constraint("uq_assembly_agent_execution", type_="unique")
        batch.drop_column("execution_key")
        batch.alter_column("trace_ref", existing_type=sa.String(40), nullable=False)
    with op.batch_alter_table("assembly_operations") as batch:
        batch.alter_column("trace_ref", existing_type=sa.String(40), nullable=False)
    for table, column, old_column, old_type, unique in reversed(_LINKS):
        with op.batch_alter_table(table) as batch:
            if old_column is not None:
                batch.drop_constraint(f"ck_{table}_authority_root", type_="check")
                batch.alter_column(old_column, existing_type=old_type, nullable=False)
            if unique:
                batch.drop_constraint(f"uq_{table}_{column}", type_="unique")
            batch.drop_constraint(f"fk_{table}_{column}", type_="foreignkey")
            batch.drop_column(column)
    with op.batch_alter_table("semantic_request_specifications") as batch:
        batch.drop_constraint("ck_request_specifications_schema", type_="check")
        batch.drop_constraint("ck_request_specifications_interpretation", type_="check")
        batch.create_check_constraint("ck_request_specifications_schema", "schema_version = 1")
        batch.create_check_constraint(
            "ck_request_specifications_interpretation",
            "interpretation_state = 'pending_evidence_validation'",
        )

"""add durable incremental changeset assembly

Revision ID: 20260906a5d2
Revises: 20260905f4c1
Create Date: 2026-09-06 14:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906a5d2"
down_revision: str | None = "20260905f4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("change_sets") as batch_op:
        batch_op.add_column(
            sa.Column("normalized_entries_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column(
                "compiled_action_ownership_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column("compiler_manifest_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("commit_receipt_json", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("change_set_revisions") as batch_op:
        batch_op.add_column(
            sa.Column("normalized_entries_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column(
                "compiled_action_ownership_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column("compiler_manifest_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("validation_errors_json", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("semantic_request_attempts") as batch_op:
        batch_op.add_column(sa.Column("execution_trace_ref", sa.String(length=40)))
        batch_op.add_column(sa.Column("observed_changeset_ref", sa.String(length=40)))
        batch_op.add_column(sa.Column("observed_draft_revision", sa.Integer()))
        batch_op.create_unique_constraint(
            "uq_semantic_request_attempts_execution_trace",
            ["semantic_request_id", "execution_trace_ref"],
        )
    op.create_table(
        "assembly_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_utterance_ref", sa.String(length=40), nullable=False),
        sa.Column("trace_ref", sa.String(length=40), nullable=False),
        sa.Column("semantic_request_ref", sa.String(length=40), nullable=True),
        sa.Column("semantic_request_attempt_ref", sa.String(length=40), nullable=True),
        sa.Column("next_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["semantic_request_attempt_ref"],
            ["semantic_request_attempts.ref_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_request_ref"], ["semantic_requests.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_utterance_ref"], ["operator_utterances.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "semantic_request_attempt_ref", name="uq_assembly_executions_semantic_attempt"
        ),
        sa.UniqueConstraint(
            "source_utterance_ref", "trace_ref", name="uq_assembly_executions_utterance_trace"
        ),
    )
    op.create_index(
        "ix_assembly_executions_trace", "assembly_executions", ["trace_ref"], unique=False
    )
    op.create_table(
        "assembly_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assembly_execution_id", sa.Uuid(), nullable=False),
        sa.Column("source_utterance_ref", sa.String(length=40), nullable=False),
        sa.Column("trace_ref", sa.String(length=40), nullable=False),
        sa.Column("upstream_tool_call_id", sa.String(length=255), nullable=False),
        sa.Column("operation_key", sa.String(length=512), nullable=False),
        sa.Column("operation_kind", sa.String(length=16), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("argument_hash", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt_sequence", sa.Integer(), nullable=False),
        sa.Column("causal_observed_revision", sa.Integer(), nullable=True),
        sa.Column("semantic_request_ref", sa.String(length=40), nullable=True),
        sa.Column("semantic_request_attempt_ref", sa.String(length=40), nullable=True),
        sa.Column("change_set_ref", sa.String(length=40), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="admitted"),
        sa.Column("result_disposition", sa.String(length=64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "operation_kind IN ('stage', 'review', 'commit')",
            name="ck_assembly_operations_kind",
        ),
        sa.CheckConstraint(
            "state IN ('admitted', 'running', 'completed', 'rejected', 'unknown')",
            name="ck_assembly_operations_state",
        ),
        sa.ForeignKeyConstraint(
            ["assembly_execution_id"], ["assembly_executions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["semantic_request_attempt_ref"],
            ["semantic_request_attempts.ref_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_request_ref"], ["semantic_requests.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_utterance_ref"], ["operator_utterances.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assembly_execution_id",
            "attempt_sequence",
            name="uq_assembly_operations_execution_sequence",
        ),
        sa.UniqueConstraint("operation_key", name="uq_assembly_operations_key"),
        sa.UniqueConstraint(
            "source_utterance_ref",
            "upstream_tool_call_id",
            name="uq_assembly_operations_utterance_call",
        ),
    )
    op.create_index(
        "ix_assembly_operations_execution_state",
        "assembly_operations",
        ["assembly_execution_id", "state"],
        unique=False,
    )
    with op.batch_alter_table("change_set_revisions") as batch_op:
        batch_op.add_column(sa.Column("assembly_operation_id", sa.Uuid()))
        batch_op.create_foreign_key(
            "fk_change_set_revisions_assembly_operation",
            "assembly_operations",
            ["assembly_operation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_change_set_revisions_assembly_operation",
            ["assembly_operation_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("change_set_revisions") as batch_op:
        batch_op.drop_constraint(
            "uq_change_set_revisions_assembly_operation",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_change_set_revisions_assembly_operation",
            type_="foreignkey",
        )
        batch_op.drop_column("assembly_operation_id")
    op.drop_index("ix_assembly_operations_execution_state", table_name="assembly_operations")
    op.drop_table("assembly_operations")
    op.drop_index("ix_assembly_executions_trace", table_name="assembly_executions")
    op.drop_table("assembly_executions")
    with op.batch_alter_table("semantic_request_attempts") as batch_op:
        batch_op.drop_constraint(
            "uq_semantic_request_attempts_execution_trace",
            type_="unique",
        )
        batch_op.drop_column("observed_draft_revision")
        batch_op.drop_column("observed_changeset_ref")
        batch_op.drop_column("execution_trace_ref")
    with op.batch_alter_table("change_set_revisions") as batch_op:
        batch_op.drop_column("validation_errors_json")
        batch_op.drop_column("compiler_manifest_json")
        batch_op.drop_column("compiled_action_ownership_json")
        batch_op.drop_column("normalized_entries_json")
    with op.batch_alter_table("change_sets") as batch_op:
        batch_op.drop_column("commit_receipt_json")
        batch_op.drop_column("compiler_manifest_json")
        batch_op.drop_column("compiled_action_ownership_json")
        batch_op.drop_column("normalized_entries_json")

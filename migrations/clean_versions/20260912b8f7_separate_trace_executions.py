"""separate admitted executions without replacing conversational evidence

Revision ID: 20260912b8f7
Revises: 20260912a7e6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260912b8f7"
down_revision: str | None = "20260912a7e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BINDING_FIELDS = (
    "id",
    "trace_ref",
    "execution_index",
    "execution_lease_id",
    "binding_basis",
    "gateway_instance_ref",
    "tool_contract_version",
    "tool_contract_hash",
    "caller_profile",
    "started_at",
)
_MOVED = (
    "gateway_instance_ref",
    "tool_contract_version",
    "tool_contract_hash",
    "caller_profile",
    "calls",
    "last_ordinal",
)


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    op.create_table(
        "trace_execution_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trace_ref",
            sa.String(40),
            sa.ForeignKey("conversational_tool_traces.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("execution_index", sa.Integer(), nullable=False),
        sa.Column(
            "execution_lease_id",
            sa.Uuid(),
            sa.ForeignKey("execution_leases.id", ondelete="RESTRICT"),
        ),
        sa.Column("binding_basis", sa.String(32), nullable=False),
        sa.Column("gateway_instance_ref", sa.String(40)),
        sa.Column("tool_contract_version", sa.String(128), nullable=False),
        sa.Column("tool_contract_hash", sa.String(64), nullable=False),
        sa.Column("caller_profile", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("calls", sa.JSON(), nullable=False),
        sa.Column("last_ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint("trace_ref", "execution_index", name="uq_trace_execution_index"),
        sa.UniqueConstraint("execution_lease_id", name="uq_trace_execution_lease"),
        sa.CheckConstraint("execution_index >= 1", name="ck_trace_execution_index"),
        sa.CheckConstraint("last_ordinal >= 0", name="ck_trace_execution_ordinal"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'interrupted')",
            name="ck_trace_execution_status",
        ),
        sa.CheckConstraint(
            "(binding_basis = 'admitted_execution' AND execution_lease_id IS NOT NULL "
            "AND gateway_instance_ref IS NOT NULL) OR "
            "(binding_basis = 'retained_trace' AND execution_lease_id IS NULL)",
            name="ck_trace_execution_binding",
        ),
    )
    for field in ("trace_ref", "gateway_instance_ref"):
        op.create_index(f"ix_trace_execution_segments_{field}", "trace_execution_segments", [field])
    # This is a lossless move of actual captured provenance, not reconstruction
    # of past execution claims. Source identity, payloads, times and outcomes stay
    # unchanged; no historical segment is assigned a guessed ExecutionLease.
    op.execute(
        sa.text(
            "INSERT INTO trace_execution_segments (id, trace_ref, execution_index, binding_basis, "
            "gateway_instance_ref, tool_contract_version, tool_contract_hash, caller_profile, "
            "started_at, completed_at, status, calls, last_ordinal) "
            "SELECT id, ref_id, 1, 'retained_trace', gateway_instance_ref, tool_contract_version, "
            "tool_contract_hash, caller_profile, started_at, completed_at, status, calls, last_ordinal "
            "FROM conversational_tool_traces"
        )
    )
    op.alter_column("semantic_request_attempts", "execution_trace_ref", new_column_name="trace_ref")
    for table in (
        "tool_invocations",
        "assembly_executions",
        "semantic_request_attempts",
        "trace_timing_observations",
    ):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("trace_execution_id", sa.Uuid()))
            batch.create_foreign_key(
                f"fk_{table}_trace_execution",
                "trace_execution_segments",
                ["trace_execution_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        if table == "trace_timing_observations" and postgres:
            op.execute(
                "ALTER TABLE trace_timing_observations DISABLE TRIGGER "
                "trg_trace_timing_observations_immutable"
            )
        op.execute(
            sa.text(
                f"UPDATE {table} SET trace_execution_id = (SELECT id FROM trace_execution_segments "
                f"WHERE trace_execution_segments.trace_ref = {table}.trace_ref) "
                "WHERE trace_ref IS NOT NULL"
            )
        )
        if table == "trace_timing_observations":
            with op.batch_alter_table(table) as batch:
                batch.alter_column("trace_execution_id", nullable=False)
            if postgres:
                op.execute(
                    "ALTER TABLE trace_timing_observations ENABLE TRIGGER "
                    "trg_trace_timing_observations_immutable"
                )
        if table in {"tool_invocations", "trace_timing_observations"}:
            op.create_index(f"ix_{table}_trace_execution_id", table, ["trace_execution_id"])
    for table, old, new, fields in (
        (
            "tool_invocations",
            "uq_tool_invocations_trace_call",
            "uq_tool_invocations_execution_call",
            ["trace_execution_id", "trace_call_id"],
        ),
        (
            "assembly_executions",
            "uq_assembly_executions_utterance_trace",
            "uq_assembly_executions_trace_execution",
            ["trace_execution_id"],
        ),
        (
            "semantic_request_attempts",
            "uq_semantic_request_attempts_execution_trace",
            "uq_semantic_request_attempts_execution",
            ["semantic_request_id", "trace_execution_id"],
        ),
        (
            "assembly_operations",
            "uq_assembly_operations_utterance_call",
            "uq_assembly_operations_execution_call",
            ["assembly_execution_id", "upstream_tool_call_id"],
        ),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(old, type_="unique")
            batch.create_unique_constraint(new, fields)
    with op.batch_alter_table("conversational_tool_traces") as batch:
        batch.drop_constraint("ck_conversational_tool_traces_last_ordinal", type_="check")
        for field in _MOVED:
            batch.drop_column(field)
    if not postgres:
        return
    immutable = " OR ".join(
        f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in _BINDING_FIELDS
    )
    op.execute(f"""
        CREATE FUNCTION docket_protect_trace_execution_binding() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'trace execution evidence cannot be deleted';
            END IF;
            IF {immutable} THEN
                RAISE EXCEPTION 'trace execution binding is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER trg_trace_execution_binding BEFORE UPDATE OR DELETE "
        "ON trace_execution_segments FOR EACH ROW "
        "EXECUTE FUNCTION docket_protect_trace_execution_binding()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM trace_execution_segments "
            "WHERE binding_basis != 'retained_trace')"
        )
    ):
        raise RuntimeError("Admitted execution segments exist; downgrade would lose provenance")
    # Reversible only before new execution work: restore the exact old shape.
    for field, kind, nullable in (
        ("gateway_instance_ref", sa.String(40), True),
        ("tool_contract_version", sa.String(128), False),
        ("tool_contract_hash", sa.String(64), False),
        ("caller_profile", sa.String(32), False),
        ("calls", sa.JSON(), False),
        ("last_ordinal", sa.Integer(), False),
    ):
        op.add_column("conversational_tool_traces", sa.Column(field, kind, nullable=True))
        op.execute(
            sa.text(
                f"UPDATE conversational_tool_traces SET {field} = (SELECT {field} "
                "FROM trace_execution_segments WHERE trace_execution_segments.trace_ref = "
                "conversational_tool_traces.ref_id AND execution_index = 1)"
            )
        )
        with op.batch_alter_table("conversational_tool_traces") as batch:
            batch.alter_column(field, nullable=nullable)
    with op.batch_alter_table("conversational_tool_traces") as batch:
        batch.create_check_constraint(
            "ck_conversational_tool_traces_last_ordinal", "last_ordinal >= 0"
        )
    for table, new, old, fields in (
        (
            "tool_invocations",
            "uq_tool_invocations_execution_call",
            "uq_tool_invocations_trace_call",
            ["trace_ref", "trace_call_id"],
        ),
        (
            "assembly_executions",
            "uq_assembly_executions_trace_execution",
            "uq_assembly_executions_utterance_trace",
            ["source_utterance_ref", "trace_ref"],
        ),
        (
            "semantic_request_attempts",
            "uq_semantic_request_attempts_execution",
            "uq_semantic_request_attempts_execution_trace",
            ["semantic_request_id", "trace_ref"],
        ),
        (
            "assembly_operations",
            "uq_assembly_operations_execution_call",
            "uq_assembly_operations_utterance_call",
            ["source_utterance_ref", "upstream_tool_call_id"],
        ),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(new, type_="unique")
            batch.create_unique_constraint(old, fields)
    for table in (
        "tool_invocations",
        "assembly_executions",
        "semantic_request_attempts",
        "trace_timing_observations",
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_trace_execution", type_="foreignkey")
            if table in {"tool_invocations", "trace_timing_observations"}:
                batch.drop_index(f"ix_{table}_trace_execution_id")
            batch.drop_column("trace_execution_id")
    op.alter_column("semantic_request_attempts", "trace_ref", new_column_name="execution_trace_ref")
    op.drop_table("trace_execution_segments")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_protect_trace_execution_binding()")

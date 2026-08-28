"""Separate tool transport completion from authoritative domain outcomes.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rewrite_trace_calls(bind: Connection, *, downgrade: bool) -> None:
    traces = sa.Table("discord_mcp_traces", sa.MetaData(), autoload_with=bind)
    for row in bind.execute(sa.select(traces.c.id, traces.c.calls)):
        calls = [dict(item) for item in list(row.calls or [])]
        rewritten: list[dict[str, Any]] = []
        for call in calls:
            if downgrade:
                transport_state = str(
                    call.pop("transport_state", call.get("state", "failed"))
                )
                domain_state = str(call.pop("domain_state", "unknown"))
                call["state"] = (
                    "succeeded"
                    if transport_state == "completed" and domain_state == "succeeded"
                    else transport_state
                    if transport_state in {"running", "timed_out"}
                    else "failed"
                )
                call["error_code"] = call.pop("transport_error_code", None)
                call.pop("domain_error_code", None)
                call.pop("tool_call_ref", None)
                call.pop("received_argument_hash", None)
                call.pop("disposition", None)
            else:
                legacy_state = str(call.pop("state", "failed"))
                call["transport_state"] = (
                    "completed" if legacy_state == "succeeded" else legacy_state
                )
                call["domain_state"] = "unknown"
                call["transport_error_code"] = call.pop("error_code", None)
                call["domain_error_code"] = None
                call["tool_call_ref"] = None
                call.setdefault("received_argument_hash", None)
                call["disposition"] = None
            rewritten.append(call)
        if calls:
            bind.execute(
                traces.update().where(traces.c.id == row.id).values(calls=rewritten)
            )


def upgrade() -> None:
    with op.batch_alter_table("tool_invocations") as batch:
        batch.add_column(sa.Column("result_disposition", sa.String(length=64)))
    _rewrite_trace_calls(op.get_bind(), downgrade=False)


def downgrade() -> None:
    _rewrite_trace_calls(op.get_bind(), downgrade=True)
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_column("result_disposition")

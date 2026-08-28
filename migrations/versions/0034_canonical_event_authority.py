"""Add ChangeSet authority and provenance to canonical events.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.TextClause:
    return sa.text("'[]'")


def upgrade() -> None:
    with op.batch_alter_table("canonical_events") as batch:
        batch.add_column(sa.Column("lane_id", sa.Uuid()))
        batch.add_column(sa.Column("lane_ref", sa.String(length=40)))
        batch.add_column(sa.Column("routing_decision_ref", sa.String(length=40)))
        batch.add_column(sa.Column("operator_policy_text", sa.Text()))
        batch.add_column(
            sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(sa.Column("created_by_changeset_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column(
                "provenance_status",
                sa.String(length=32),
                server_default="legacy_preledger",
                nullable=False,
            )
        )
        batch.create_foreign_key(
            "fk_canonical_events_lane",
            "calendar_lanes",
            ["lane_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_canonical_events_provenance_status",
            "provenance_status IN ('complete', 'legacy_preledger')",
        )

    bind = op.get_bind()
    events = sa.table(
        "canonical_events",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
        sa.column("canonical_key", sa.String()),
        sa.column("title", sa.String()),
        sa.column("calendar_lane", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("basis_refs", sa.JSON()),
        sa.column("source_refs", sa.JSON()),
    )
    sources = sa.table(
        "provenance_sources",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
        sa.column("source_kind", sa.String()),
        sa.column("external_ref", sa.String()),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("content_hash", sa.String()),
        sa.column("metadata_json", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    source_rows: list[dict[str, Any]] = []
    for event in bind.execute(sa.select(events)):
        source_ref = f"src_{event.ref_id.split('_', 1)[1]}"
        metadata = {
            "event_ref": event.ref_id,
            "canonical_key": event.canonical_key,
            "title": event.title,
            "calendar_lane": event.calendar_lane,
            "legacy_status": event.status,
        }
        serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        source_rows.append(
            {
                "id": uuid.uuid4(),
                "ref_id": source_ref,
                "source_kind": "legacy_canonical_object",
                "external_ref": event.ref_id,
                "observed_at": event.created_at or now,
                "content_hash": hashlib.sha256(serialized.encode()).hexdigest(),
                "metadata_json": metadata,
                "created_at": now,
            }
        )
        bind.execute(
            events.update()
            .where(events.c.id == event.id)
            .values(basis_refs=[source_ref], source_refs=[source_ref])
        )
    if source_rows:
        op.bulk_insert(sources, source_rows)


def downgrade() -> None:
    is_postgresql = op.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_provenance_sources_immutable "
            "ON provenance_sources"
        )
    op.execute(
        "DELETE FROM provenance_sources WHERE source_kind = "
        "'legacy_canonical_object' AND external_ref IN "
        "(SELECT ref_id FROM canonical_events)"
    )
    if is_postgresql:
        op.execute(
            "CREATE TRIGGER trg_provenance_sources_immutable BEFORE UPDATE OR DELETE "
            "ON provenance_sources FOR EACH ROW EXECUTE FUNCTION "
            "docket_reject_immutable_provenance()"
        )
    with op.batch_alter_table("canonical_events") as batch:
        batch.drop_constraint("ck_canonical_events_provenance_status", type_="check")
        batch.drop_constraint("fk_canonical_events_lane", type_="foreignkey")
        batch.drop_column("provenance_status")
        batch.drop_column("created_by_changeset_ref")
        batch.drop_column("source_refs")
        batch.drop_column("decision_refs")
        batch.drop_column("basis_refs")
        batch.drop_column("operator_policy_text")
        batch.drop_column("routing_decision_ref")
        batch.drop_column("lane_ref")
        batch.drop_column("lane_id")

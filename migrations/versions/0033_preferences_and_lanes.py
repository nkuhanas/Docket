"""Add structured Preferences and first-class Calendar routing policy.

Revision ID: 0033
Revises: 0032
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

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.TextClause:
    return sa.text("'[]'")


def _json_object() -> sa.TextClause:
    return sa.text("'{}'")


def _canonical_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("preference_key", sa.String(length=255), nullable=False),
        sa.Column("policy_kind", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_ref", sa.String(length=40)),
        sa.Column("target_key", sa.String(length=1024)),
        sa.Column("semantic_class", sa.String(length=64)),
        sa.Column("policy_text", sa.Text(), nullable=False),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_canonical_columns(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "policy_kind IN ('behavior', 'suppression', 'calendar_route')",
            name="ck_preferences_policy_kind",
        ),
        sa.CheckConstraint(
            "target_type IN ('global', 'entity', 'identity', 'source', "
            "'semantic_class')",
            name="ck_preferences_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_preferences_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("preference_key", name="uq_preferences_key"),
    )
    op.create_table(
        "lane_routing_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("lane_id", sa.Uuid(), nullable=False),
        sa.Column("lane_ref", sa.String(length=40), nullable=False),
        sa.Column("event_ref", sa.String(length=40)),
        sa.Column("organization_ref", sa.String(length=40)),
        sa.Column("recurring_identity", sa.String(length=512)),
        sa.Column("decision_kind", sa.String(length=32), nullable=False),
        sa.Column("applicability_scope", sa.JSON(), nullable=False),
        sa.Column("operator_confirmed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_canonical_columns(),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "decision_kind IN ('explicit_operator', 'structured_preference', "
            "'entity_rule', 'historical_precedent', 'semantic_inference')",
            name="ck_lane_routing_decisions_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_lane_routing_decisions_status",
        ),
        sa.ForeignKeyConstraint(
            ["lane_id"], ["calendar_lanes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_lane_routes_organization_decided",
        "lane_routing_decisions",
        ["organization_ref", "decided_at"],
    )
    op.create_index(
        "ix_lane_routes_recurring_decided",
        "lane_routing_decisions",
        ["recurring_identity", "decided_at"],
    )
    with op.batch_alter_table("calendar_lanes") as batch:
        batch.add_column(sa.Column("operator_policy_text", sa.Text()))
        batch.add_column(
            sa.Column(
                "metadata_json",
                sa.JSON(),
                server_default=_json_object(),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.add_column(
            sa.Column("priority", sa.Integer(), server_default="100", nullable=False)
        )
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
        batch.create_check_constraint(
            "ck_calendar_lanes_provenance_status",
            "provenance_status IN ('complete', 'legacy_preledger')",
        )

    bind = op.get_bind()
    lanes = sa.table(
        "calendar_lanes",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
        sa.column("lane", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("enabled", sa.Boolean()),
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
    for lane in bind.execute(sa.select(lanes)):
        source_ref = f"src_{lane.ref_id.split('_', 1)[1]}"
        metadata = {
            "lane_ref": lane.ref_id,
            "lane": lane.lane,
            "display_name": lane.display_name,
            "legacy_status": lane.status,
        }
        serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        source_rows.append(
            {
                "id": uuid.uuid4(),
                "ref_id": source_ref,
                "source_kind": "legacy_canonical_object",
                "external_ref": lane.ref_id,
                "observed_at": lane.created_at or now,
                "content_hash": hashlib.sha256(serialized.encode()).hexdigest(),
                "metadata_json": metadata,
                "created_at": now,
            }
        )
        bind.execute(
            lanes.update()
            .where(lanes.c.id == lane.id)
            .values(
                enabled=lane.status != "deleted",
                basis_refs=[source_ref],
                source_refs=[source_ref],
            )
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
        "(SELECT ref_id FROM calendar_lanes)"
    )
    if is_postgresql:
        op.execute(
            "CREATE TRIGGER trg_provenance_sources_immutable BEFORE UPDATE OR DELETE "
            "ON provenance_sources FOR EACH ROW EXECUTE FUNCTION "
            "docket_reject_immutable_provenance()"
        )
    with op.batch_alter_table("calendar_lanes") as batch:
        batch.drop_constraint("ck_calendar_lanes_provenance_status", type_="check")
        batch.drop_column("provenance_status")
        batch.drop_column("created_by_changeset_ref")
        batch.drop_column("source_refs")
        batch.drop_column("decision_refs")
        batch.drop_column("basis_refs")
        batch.drop_column("priority")
        batch.drop_column("enabled")
        batch.drop_column("metadata_json")
        batch.drop_column("operator_policy_text")
    op.drop_index(
        "ix_lane_routes_recurring_decided", table_name="lane_routing_decisions"
    )
    op.drop_index(
        "ix_lane_routes_organization_decided", table_name="lane_routing_decisions"
    )
    op.drop_table("lane_routing_decisions")
    op.drop_table("preferences")

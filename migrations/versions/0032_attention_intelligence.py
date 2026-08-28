"""Add non-authoritative triage runs, context packets, and AttentionCases.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.TextClause:
    return sa.text("'[]'")


def upgrade() -> None:
    op.create_table(
        "triage_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.String(length=255), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("context_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("contract_version", sa.String(length=128), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(length=128)),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_triage_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_table(
        "context_packets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("triage_run_id", sa.Uuid(), nullable=False),
        sa.Column("triage_run_ref", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=40), nullable=False),
        sa.Column("trusted_context_json", sa.JSON(), nullable=False),
        sa.Column("serialized_bytes", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=128), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "serialized_bytes <= 32768", name="ck_context_packets_byte_budget"
        ),
        sa.ForeignKeyConstraint(
            ["triage_run_id"], ["triage_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "triage_run_id", "source_ref", name="uq_context_packets_run_source"
        ),
    )
    op.create_table(
        "attention_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("situation_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("semantic_classes", sa.JSON(), nullable=False),
        sa.Column("entity_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("queue_item_id", sa.Uuid()),
        sa.Column("resolution_decision_ref", sa.String(length=40)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'suppressed', 'cancelled')",
            name="ck_attention_cases_status",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_attention_cases_priority",
        ),
        sa.ForeignKeyConstraint(["queue_item_id"], ["queue_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint("situation_key", name="uq_attention_cases_situation"),
    )
    op.create_table(
        "attention_case_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("attention_case_id", sa.Uuid(), nullable=False),
        sa.Column("case_ref", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("semantic_classes", sa.JSON(), nullable=False),
        sa.Column("item_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attention_case_id"], ["attention_cases.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "attention_case_id", "revision", name="uq_attention_case_revision"
        ),
    )
    op.create_table(
        "case_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("attention_case_id", sa.Uuid(), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("candidate_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "item_type IN ('person_resolution', 'organization_resolution', "
            "'identity_resolution', 'affiliation_candidate', "
            "'relationship_candidate', 'fact_candidate', 'event_candidate', "
            "'lane_resolution', 'preference_match', 'decision_required')",
            name="ck_case_items_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'rejected')",
            name="ck_case_items_status",
        ),
        sa.ForeignKeyConstraint(
            ["attention_case_id"], ["attention_cases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "attention_case_id", "item_key", name="uq_case_items_case_key"
        ),
    )
    op.create_table(
        "case_sources",
        sa.Column("attention_case_id", sa.Uuid(), nullable=False),
        sa.Column("source_ref", sa.String(length=40), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attention_case_id"], ["attention_cases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("attention_case_id", "source_ref"),
    )
    op.create_table(
        "triage_brief_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("triage_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_ref", sa.String(length=40), nullable=False),
        sa.Column("semantic_classes", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255)),
        sa.Column("included_brief_ref", sa.String(length=40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "disposition IN ('include', 'suppress')",
            name="ck_triage_brief_entries_disposition",
        ),
        sa.ForeignKeyConstraint(
            ["triage_run_id"], ["triage_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_table(
        "daily_brief_case_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brief_id", sa.Uuid(), nullable=False),
        sa.Column("attention_case_id", sa.Uuid(), nullable=False),
        sa.Column("case_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("section", sa.String(length=64), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["brief_id"], ["daily_briefs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attention_case_id"], ["attention_cases.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "brief_id", "attention_case_id", name="uq_daily_brief_case_item"
        ),
    )
    with op.batch_alter_table("queue_items") as batch:
        batch.add_column(sa.Column("attention_case_ref", sa.String(length=40)))
        batch.add_column(sa.Column("attention_case_revision_ref", sa.String(length=40)))
        batch.add_column(sa.Column("daily_brief_ref", sa.String(length=40)))
    with op.batch_alter_table("daily_briefs") as batch:
        batch.add_column(sa.Column("interval_start", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("interval_end", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("case_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("projection_revision", sa.Integer(), server_default="1", nullable=False)
        )
    op.execute(
        "UPDATE daily_briefs SET interval_start = triage_windows.starts_at, "
        "interval_end = triage_windows.ends_at FROM triage_windows "
        "WHERE daily_briefs.window_id = triage_windows.id"
        if op.get_bind().dialect.name == "postgresql"
        else "UPDATE daily_briefs SET interval_start = (SELECT starts_at FROM "
        "triage_windows WHERE triage_windows.id = daily_briefs.window_id), "
        "interval_end = (SELECT ends_at FROM triage_windows WHERE "
        "triage_windows.id = daily_briefs.window_id)"
    )
    with op.batch_alter_table("discord_projections") as batch:
        batch.add_column(sa.Column("primary_public_ref", sa.String(length=40)))
        batch.add_column(sa.Column("primary_revision_ref", sa.String(length=40)))


def downgrade() -> None:
    with op.batch_alter_table("discord_projections") as batch:
        batch.drop_column("primary_revision_ref")
        batch.drop_column("primary_public_ref")
    with op.batch_alter_table("daily_briefs") as batch:
        batch.drop_column("projection_revision")
        batch.drop_column("basis_refs")
        batch.drop_column("case_refs")
        batch.drop_column("interval_end")
        batch.drop_column("interval_start")
    with op.batch_alter_table("queue_items") as batch:
        batch.drop_column("daily_brief_ref")
        batch.drop_column("attention_case_revision_ref")
        batch.drop_column("attention_case_ref")
    op.drop_table("daily_brief_case_items")
    op.drop_table("triage_brief_entries")
    op.drop_table("case_sources")
    op.drop_table("case_items")
    op.drop_table("attention_case_revisions")
    op.drop_table("attention_cases")
    op.drop_table("context_packets")
    op.drop_table("triage_runs")

"""add canonical recurrence occurrence identity

Revision ID: 20260911b1e0
Revises: 20260906a5d2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911b1e0"
down_revision: str | None = "20260906a5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_cache", sa.Column("original_start_date", sa.Date(), nullable=True)
    )
    op.create_table(
        "calendar_date_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("utterance_ref", sa.String(40), nullable=False),
        sa.Column("relative_day", sa.String(16), nullable=False),
        sa.Column("resolved_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["utterance_ref"], ["operator_utterances.ref_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("utterance_ref", "relative_day", name="uq_calendar_date_binding"),
        sa.CheckConstraint(
            "relative_day IN ('today', 'tomorrow')", name="ck_calendar_relative_day"
        ),
    )
    op.create_table(
        "event_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("series_ref", sa.String(40), nullable=False),
        sa.Column("original_start_key", sa.String(128), nullable=False),
        sa.Column("original_local_date", sa.Date(), nullable=False),
        sa.Column("original_timezone", sa.String(128), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("original_timing_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("replacement_event_ref", sa.String(40), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("last_changeset_ref", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["series_ref"], ["canonical_events.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["replacement_event_ref"], ["canonical_events.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "series_ref", "original_start_key", name="uq_event_occurrences_identity"
        ),
        sa.UniqueConstraint("replacement_event_ref", name="uq_event_occurrences_replacement"),
        sa.CheckConstraint(
            "status IN ('cancelled', 'replaced')", name="ck_event_occurrences_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_event_occurrences_version"),
        sa.CheckConstraint(
            "status <> 'replaced' OR replacement_event_ref IS NOT NULL",
            name="ck_event_occurrences_replacement_required",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION docket_preserve_calendar_date_binding() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'calendar date binding is immutable';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER preserve_calendar_date_binding BEFORE UPDATE OR DELETE ON calendar_date_bindings
                FOR EACH ROW EXECUTE FUNCTION docket_preserve_calendar_date_binding();
            CREATE FUNCTION docket_preserve_occurrence_identity() RETURNS trigger AS $$
            BEGIN
                IF (OLD.series_ref, OLD.original_start_key, OLD.original_local_date,
                    OLD.original_timezone, OLD.identity_json::jsonb, OLD.original_timing_json::jsonb)
                   IS DISTINCT FROM
                   (NEW.series_ref, NEW.original_start_key, NEW.original_local_date,
                    NEW.original_timezone, NEW.identity_json::jsonb, NEW.original_timing_json::jsonb)
                THEN
                    RAISE EXCEPTION 'canonical occurrence identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER preserve_occurrence_identity BEFORE UPDATE ON event_occurrences
                FOR EACH ROW EXECUTE FUNCTION docket_preserve_occurrence_identity();
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER preserve_calendar_date_binding ON calendar_date_bindings")
        op.execute("DROP FUNCTION docket_preserve_calendar_date_binding()")
        op.execute("DROP TRIGGER preserve_occurrence_identity ON event_occurrences")
        op.execute("DROP FUNCTION docket_preserve_occurrence_identity()")
    op.drop_table("event_occurrences")
    op.drop_table("calendar_date_bindings")
    op.drop_column("calendar_event_cache", "original_start_date")

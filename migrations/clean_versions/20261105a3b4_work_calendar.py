"""retain opt-in work Calendar snapshots and source invalidation

Revision ID: 20261105a3b4
Revises: 20261105f2a3
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105a3b4"
down_revision = "20261105f2a3"
branch_labels = None
depends_on = None


def state_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "source_revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "observed_work_revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "event_ref",
            sa.String(40),
            sa.ForeignKey("canonical_events.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "calendar_ref",
            sa.String(40),
            sa.ForeignKey("calendar_lanes.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("needs_refresh", sa.Boolean(), nullable=False),
        sa.Column("renderer_version", sa.Integer(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "work_calendar_projections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), unique=True, nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column(
            "work_ref",
            sa.String(40),
            sa.ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"),
            unique=True,
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        *state_columns(),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_work_calendar_version"),
    )
    op.create_table(
        "work_calendar_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), unique=True, nullable=False),
        sa.Column(
            "projection_ref",
            sa.String(40),
            sa.ForeignKey("work_calendar_projections.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        *state_columns(),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("projection_ref", "version", name="uq_work_calendar_revision"),
        sa.CheckConstraint("version >= 1", name="ck_work_calendar_revision_version"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION retain_work_calendar_revision() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'work Calendar history is immutable'; END $$;
            CREATE TRIGGER retain_work_calendar_revision BEFORE UPDATE OR DELETE ON work_calendar_revisions
              FOR EACH ROW EXECUTE FUNCTION retain_work_calendar_revision();
            CREATE FUNCTION guard_work_calendar_head() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'work Calendar heads are retained'; END IF;
              IF (NEW.id,NEW.ref_id,NEW.operator_ref,NEW.work_ref,NEW.created_at,NEW.created_by_changeset_ref)
                IS DISTINCT FROM (OLD.id,OLD.ref_id,OLD.operator_ref,OLD.work_ref,OLD.created_at,OLD.created_by_changeset_ref)
                OR NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'work Calendar identity or version changed improperly'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER guard_work_calendar_head BEFORE UPDATE OR DELETE ON work_calendar_projections
              FOR EACH ROW EXECUTE FUNCTION guard_work_calendar_head();
            CREATE FUNCTION guard_work_calendar_revision() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE head work_calendar_projections;
            BEGIN
              SELECT * INTO head FROM work_calendar_projections WHERE ref_id = NEW.projection_ref;
              IF head.version IS DISTINCT FROM NEW.version OR
                (head.source_revision_ref,head.observed_work_revision_ref,head.event_ref,head.calendar_ref,
                  head.enabled,head.needs_refresh,head.renderer_version) IS DISTINCT FROM
                (NEW.source_revision_ref,NEW.observed_work_revision_ref,NEW.event_ref,NEW.calendar_ref,
                  NEW.enabled,NEW.needs_refresh,NEW.renderer_version) THEN
                RAISE EXCEPTION 'work Calendar revision must match its exact head'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER guard_work_calendar_revision BEFORE INSERT ON work_calendar_revisions
              FOR EACH ROW EXECUTE FUNCTION guard_work_calendar_revision();
            CREATE FUNCTION require_work_calendar_revision() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE head work_calendar_projections; retained work_calendar_revisions;
              work work_sessions; pinned work_session_revisions; observed work_session_revisions;
              display canonical_events;
            BEGIN
              SELECT * INTO head FROM work_calendar_projections WHERE ref_id = NEW.ref_id;
              SELECT * INTO retained FROM work_calendar_revisions WHERE projection_ref = head.ref_id AND version = head.version;
              SELECT * INTO work FROM work_sessions WHERE ref_id = head.work_ref;
              SELECT * INTO pinned FROM work_session_revisions WHERE ref_id = head.source_revision_ref;
              SELECT * INTO observed FROM work_session_revisions WHERE ref_id = head.observed_work_revision_ref;
              SELECT * INTO display FROM canonical_events WHERE ref_id = head.event_ref;
              IF retained.ref_id IS NULL OR
                (head.source_revision_ref,head.observed_work_revision_ref,head.event_ref,head.calendar_ref,
                  head.enabled,head.needs_refresh,head.renderer_version) IS DISTINCT FROM
                (retained.source_revision_ref,retained.observed_work_revision_ref,retained.event_ref,retained.calendar_ref,
                  retained.enabled,retained.needs_refresh,retained.renderer_version) OR
                head.operator_ref IS DISTINCT FROM work.operator_ref OR
                pinned.work_ref IS DISTINCT FROM head.work_ref OR observed.work_ref IS DISTINCT FROM head.work_ref OR
                display.lane_ref IS DISTINCT FROM head.calendar_ref OR
                display.canonical_key NOT LIKE 'actual-work:' || head.work_ref || ':%' OR
                (NOT head.enabled AND display.status <> 'cancelled') OR
                (head.enabled AND observed.revision <> work.version) OR
                (head.enabled AND NOT head.needs_refresh AND head.source_revision_ref <> head.observed_work_revision_ref) THEN
                RAISE EXCEPTION 'work Calendar head requires its owned exact retained revision'; END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER require_work_calendar_revision AFTER INSERT OR UPDATE ON work_calendar_projections
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_work_calendar_revision();
            CREATE FUNCTION invalidate_work_calendar_required() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF EXISTS (SELECT 1 FROM work_calendar_projections p
                JOIN work_session_revisions r ON r.ref_id = p.observed_work_revision_ref
                JOIN work_sessions w ON w.ref_id = p.work_ref
                WHERE p.work_ref = NEW.ref_id AND p.enabled AND r.revision <> w.version) THEN
                RAISE EXCEPTION 'a corrected work record must invalidate its Calendar snapshot'; END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER invalidate_work_calendar_required AFTER UPDATE ON work_sessions
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION invalidate_work_calendar_required();
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM work_calendar_projections")):
        raise RuntimeError("cannot discard retained work Calendar history; use forward repair")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER invalidate_work_calendar_required ON work_sessions")
        op.execute("DROP FUNCTION invalidate_work_calendar_required()")
        op.execute("DROP TRIGGER require_work_calendar_revision ON work_calendar_projections")
        op.execute("DROP FUNCTION require_work_calendar_revision()")
        op.execute("DROP FUNCTION guard_work_calendar_revision() CASCADE")
        op.execute("DROP FUNCTION guard_work_calendar_head() CASCADE")
        op.execute("DROP FUNCTION retain_work_calendar_revision() CASCADE")
    op.drop_table("work_calendar_revisions")
    op.drop_table("work_calendar_projections")

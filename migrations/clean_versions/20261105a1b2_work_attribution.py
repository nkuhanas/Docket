"""add typed engagements, performed-work revisions and segments

Revision ID: 20261105a1b2
Revises: 20261104e3f4
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105a1b2"
down_revision = "20261104e3f4"
branch_labels = None
depends_on = None


def _identity() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
    ]


def _provenance() -> list[sa.Column]:
    return [
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(40),
                  sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _head() -> list[sa.Column]:
    return [
        *_identity(),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(40),
                  sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _ref(name: str, parent: str) -> sa.Column:
    return sa.Column(name, sa.String(40), sa.ForeignKey(parent, ondelete="RESTRICT"))


def upgrade() -> None:
    op.create_table("engagements", *_head(),
                    sa.CheckConstraint("version >= 1", name="ck_engagement_version"))
    op.create_index("ix_engagements_operator_ref", "engagements", ["operator_ref"])
    op.create_table(
        "engagement_revisions", *_identity(),
        sa.Column("engagement_ref", sa.String(40),
                  sa.ForeignKey("engagements.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        _ref("organization_ref", "entities.ref_id"), _ref("payer_ref", "entities.ref_id"),
        _ref("affiliation_ref", "affiliations.ref_id"),
        sa.Column("effective_start", sa.Date()), sa.Column("effective_end", sa.Date()),
        sa.Column("role", sa.String(512)), sa.Column("reporting_notes", sa.Text()),
        *_provenance(),
        sa.UniqueConstraint("engagement_ref", "revision", name="uq_engagement_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_engagement_revision"),
        sa.CheckConstraint("kind IN ('unknown', 'employment', 'contract', 'research', 'volunteer', 'personal')", name="ck_engagement_kind"),
        sa.CheckConstraint("effective_start IS NULL OR effective_end IS NULL OR effective_end >= effective_start", name="ck_engagement_dates"),
    )
    op.create_table(
        "work_sessions", *_head(),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_work_version"),
        sa.CheckConstraint("lifecycle IN ('running', 'paused', 'recorded', 'retracted')", name="ck_work_lifecycle"),
    )
    op.create_index("ix_work_sessions_operator_ref", "work_sessions", ["operator_ref"])
    op.create_index(
        "uq_work_primary_timer", "work_sessions", ["operator_ref"], unique=True,
        postgresql_where=sa.text("lifecycle IN ('running', 'paused') AND NOT superseded"),
        sqlite_where=sa.text("lifecycle IN ('running', 'paused') AND NOT superseded"),
    )
    op.create_table(
        "work_session_revisions", *_identity(),
        sa.Column("work_ref", sa.String(40),
                  sa.ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text()), sa.Column("category", sa.String(128)),
        sa.Column("time_precision", sa.String(32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("start_offset_seconds", sa.Integer()),
        sa.Column("end_offset_seconds", sa.Integer()),
        sa.Column("duration_us", sa.BigInteger()),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("work_date", sa.Date()), sa.Column("period_start", sa.Date()),
        sa.Column("period_end", sa.Date()),
        _ref("organization_ref", "entities.ref_id"), _ref("engagement_ref", "engagements.ref_id"),
        _ref("project_ref", "entities.ref_id"), _ref("item_ref", "items.ref_id"),
        _ref("task_ref", "tasks.ref_id"), _ref("event_ref", "canonical_events.ref_id"),
        _ref("place_ref", "entities.ref_id"),
        sa.Column("attribution_label", sa.String(512)),
        sa.Column("reported_compensation_class", sa.String(32)),
        sa.Column("work_mode", sa.String(32)), sa.Column("reported_location", sa.String(1000)),
        sa.Column("source_method", sa.String(32), nullable=False),
        sa.Column("uncertainty", sa.Text()), sa.Column("source_refs", sa.JSON(), nullable=False),
        *_provenance(),
        sa.UniqueConstraint("work_ref", "revision", name="uq_work_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_work_revision"),
        sa.CheckConstraint("lifecycle IN ('running', 'paused', 'recorded', 'retracted')", name="ck_work_revision_lifecycle"),
        sa.CheckConstraint("time_precision IN ('interval', 'duration', 'incomplete', 'timer')", name="ck_work_precision"),
        sa.CheckConstraint("duration_us IS NULL OR duration_us >= 0", name="ck_work_duration"),
        sa.CheckConstraint("start_at IS NULL OR end_at IS NULL OR end_at >= start_at", name="ck_work_bounds"),
        sa.CheckConstraint("period_start IS NULL OR period_end IS NULL OR period_end > period_start", name="ck_work_period"),
        sa.CheckConstraint("reported_compensation_class IS NULL OR reported_compensation_class IN ('billable', 'paid_work', 'unpaid', 'unknown')", name="ck_work_reported_class"),
        sa.CheckConstraint("work_mode IS NULL OR work_mode IN ('remote', 'onsite', 'travel')", name="ck_work_mode"),
        sa.CheckConstraint("source_method IN ('report', 'import', 'inference', 'timer')", name="ck_work_source"),
    )
    op.create_index("ix_work_revision_engagement_date", "work_session_revisions", ["engagement_ref", "work_date"])
    op.create_index("ix_work_revision_project_date", "work_session_revisions", ["project_ref", "work_date"])
    op.create_index("ix_work_revision_bounds", "work_session_revisions", ["start_at", "end_at"])
    op.create_table(
        "work_segments", sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision_id", sa.Uuid(),
                  sa.ForeignKey("work_session_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("start_offset_seconds", sa.Integer(), nullable=False),
        sa.Column("end_offset_seconds", sa.Integer()),
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_work_segment_order"),
        sa.CheckConstraint("ordinal >= 0", name="ck_work_segment_order"),
        sa.CheckConstraint("kind IN ('active', 'break')", name="ck_work_segment_kind"),
        sa.CheckConstraint("end_at IS NULL OR end_at >= start_at", name="ck_work_segment_bounds"),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in ("engagement_revisions", "work_session_revisions", "work_segments"):
            op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()")
        for table in ("engagements", "work_sessions"):
            op.execute(f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()")


def downgrade() -> None:
    tables = ("work_segments", "work_session_revisions", "work_sessions", "engagement_revisions", "engagements")
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained work or engagement history")
    for table in tables:
        op.drop_table(table)

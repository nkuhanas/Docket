"""retain exact work-report membership for stable bounded exports

Revision ID: 20261105b2c3
Revises: 20261105a1b2
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105b2c3"
down_revision = "20261105a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_report_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("principal_ref", sa.String(255), nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_work_report_snapshots_principal_ref", "work_report_snapshots", ["principal_ref"])
    op.create_table(
        "work_report_members",
        sa.Column("report_id", sa.Uuid(), sa.ForeignKey("work_report_snapshots.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("work_ref", sa.String(40), sa.ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("revision_ref", sa.String(40), sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("engagement_revision_ref", sa.String(40), sa.ForeignKey("engagement_revisions.ref_id", ondelete="RESTRICT")),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION docket_seal_work_report() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' OR OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL
                   OR (to_jsonb(NEW) - 'sealed_at') <> (to_jsonb(OLD) - 'sealed_at') THEN
                    RAISE EXCEPTION 'Work query snapshot is append-only';
                END IF;
                RETURN NEW;
            END $$
        """)
        op.execute("""
            CREATE FUNCTION docket_capture_work_report_member() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE snapshot_owner text; work_owner text; work_identity text;
            BEGIN
                SELECT operator_ref INTO snapshot_owner FROM work_report_snapshots
                    WHERE id = NEW.report_id AND sealed_at IS NULL FOR UPDATE;
                SELECT w.operator_ref, r.work_ref INTO work_owner, work_identity
                    FROM work_session_revisions r JOIN work_sessions w ON w.ref_id = r.work_ref
                    WHERE r.ref_id = NEW.revision_ref;
                IF snapshot_owner IS NULL OR snapshot_owner IS DISTINCT FROM work_owner
                   OR work_identity IS DISTINCT FROM NEW.work_ref THEN
                    RAISE EXCEPTION 'Work query membership must belong to an open owned snapshot';
                END IF;
                RETURN NEW;
            END $$
        """)
        op.execute("CREATE TRIGGER trg_work_report_seal BEFORE UPDATE OR DELETE ON work_report_snapshots FOR EACH ROW EXECUTE FUNCTION docket_seal_work_report()")
        op.execute("CREATE TRIGGER trg_work_report_member_insert BEFORE INSERT ON work_report_members FOR EACH ROW EXECUTE FUNCTION docket_capture_work_report_member()")
        op.execute("CREATE TRIGGER trg_work_report_member_immutable BEFORE UPDATE OR DELETE ON work_report_members FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()")


def downgrade() -> None:
    for table in ("work_report_members", "work_report_snapshots"):
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained work query snapshots")
    op.drop_table("work_report_members")
    op.drop_table("work_report_snapshots")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_capture_work_report_member()")
        op.execute("DROP FUNCTION docket_seal_work_report()")

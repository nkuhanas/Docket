"""retain typed work/finance follow-up without synthetic Calendar events

Revision ID: 20261105f2a3
Revises: 20261105e1f2
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105f2a3"
down_revision = "20261105e1f2"
branch_labels = None
depends_on = None

SUBJECTS = "substr(subject_ref, 1, 4) IN ('evt_','eng_','exp_','pay_') OR substr(subject_ref, 1, 5) IN ('time_','work_','fdoc_')"


def upgrade() -> None:
    with op.batch_alter_table("reminder_plans") as batch:
        batch.drop_constraint("ck_reminder_plans_subject_ref", type_="check")
        batch.create_check_constraint("ck_reminder_plans_subject_ref", SUBJECTS)
    op.create_table(
        "operational_reminders",
        sa.Column(
            "reminder_ref",
            sa.String(40),
            sa.ForeignKey("reminder_plans.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(512)),
    )
    op.create_table(
        "workload_reconciliations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("case_refs", sa.JSON(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workload_reconciliation_owner",
        "workload_reconciliations",
        ["operator_ref", "created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION retain_operational_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'operational snapshots are immutable'; END $$;
            CREATE TRIGGER retain_operational_reminder BEFORE UPDATE OR DELETE ON operational_reminders
              FOR EACH ROW EXECUTE FUNCTION retain_operational_snapshot();
            CREATE TRIGGER retain_workload_reconciliation BEFORE UPDATE OR DELETE ON workload_reconciliations
              FOR EACH ROW EXECUTE FUNCTION retain_operational_snapshot();
            CREATE FUNCTION validate_operational_reminder() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE plan reminder_plans; owned text; subject_table text;
            BEGIN
              SELECT * INTO plan FROM reminder_plans WHERE ref_id = NEW.reminder_ref;
              subject_table := CASE split_part(plan.subject_ref, '_', 1)
                WHEN 'work' THEN 'work_sessions' WHEN 'eng' THEN 'engagements'
                WHEN 'fdoc' THEN 'financial_documents' WHEN 'exp' THEN 'expenses'
                WHEN 'pay' THEN 'payments' ELSE NULL END;
              IF subject_table IS NULL OR plan.delivery_channels::jsonb <> '["docket_queue"]'::jsonb
                OR plan.lead_seconds::jsonb <> '[0]'::jsonb THEN
                RAISE EXCEPTION 'invalid operational reminder subject or delivery'; END IF;
              EXECUTE format('SELECT operator_ref FROM %I WHERE ref_id = $1', subject_table)
                INTO owned USING plan.subject_ref;
              IF owned IS DISTINCT FROM NEW.operator_ref THEN
                RAISE EXCEPTION 'operational reminder owner mismatch'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER validate_operational_reminder BEFORE INSERT ON operational_reminders
              FOR EACH ROW EXECUTE FUNCTION validate_operational_reminder();
            CREATE FUNCTION require_operational_reminder() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF split_part(NEW.subject_ref, '_', 1) IN ('work','eng','fdoc','exp','pay') AND NOT EXISTS
                (SELECT 1 FROM operational_reminders WHERE reminder_ref = NEW.ref_id) THEN
                RAISE EXCEPTION 'operational reminder requires exact retained trigger'; END IF;
              IF TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM operational_reminders WHERE reminder_ref = NEW.ref_id) AND
                (NEW.subject_ref IS DISTINCT FROM OLD.subject_ref OR NEW.timezone IS DISTINCT FROM OLD.timezone
                  OR NEW.date_trigger_local_time IS DISTINCT FROM OLD.date_trigger_local_time
                  OR NEW.delivery_channels::jsonb <> '["docket_queue"]'::jsonb
                  OR NEW.lead_seconds::jsonb <> '[0]'::jsonb) THEN
                RAISE EXCEPTION 'operational reminder identity is immutable'; END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER require_operational_reminder AFTER INSERT OR UPDATE ON reminder_plans
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_operational_reminder();
        """)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("operational_reminders", "workload_reconciliations"):
        if bind.scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(
                "cannot discard retained operational history; use forward repair"
            )
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER require_operational_reminder ON reminder_plans")
        op.execute("DROP FUNCTION require_operational_reminder()")
        op.execute("DROP FUNCTION validate_operational_reminder() CASCADE")
        op.execute("DROP FUNCTION retain_operational_snapshot() CASCADE")
    op.drop_index("ix_workload_reconciliation_owner", table_name="workload_reconciliations")
    op.drop_table("workload_reconciliations")
    op.drop_table("operational_reminders")
    with op.batch_alter_table("reminder_plans") as batch:
        batch.drop_constraint("ck_reminder_plans_subject_ref", type_="check")
        batch.create_check_constraint(
            "ck_reminder_plans_subject_ref",
            "substr(subject_ref,1,4) = 'evt_' OR substr(subject_ref,1,5) = 'time_'",
        )

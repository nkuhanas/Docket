"""retain exact expenses and independent reimbursement observations

Revision ID: 20261105b8c9
Revises: 20261105a7b8
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105b8c9"
down_revision = "20261105a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expense_report_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("principal_ref", sa.String(length=255), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("totals_json", sa.JSON(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        op.f("ix_expense_report_snapshots_principal_ref"),
        "expense_report_snapshots",
        ["principal_ref"],
        unique=False,
    )
    op.create_table(
        "expenses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.Column("external_source", sa.String(length=128), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=True),
        sa.Column("external_key_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(external_source IS NULL AND external_id IS NULL AND external_key_hash IS NULL) OR (external_source IS NOT NULL AND external_id IS NOT NULL AND external_key_hash IS NOT NULL)",
            name="ck_expense_external_identity",
        ),
        sa.CheckConstraint("version >= 1", name="ck_expense_version"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operator_ref", "external_key_hash", name="uq_expense_external_identity"
        ),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_expense_owner_updated", "expenses", ["operator_ref", "updated_at"], unique=False
    )
    op.create_table(
        "expense_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("expense_ref", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.Column("amount", sa.String(length=48), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("incurred_on", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("vendor", sa.String(length=512), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("engagement_ref", sa.String(length=40), nullable=True),
        sa.Column("organization_ref", sa.String(length=40), nullable=True),
        sa.Column("project_ref", sa.String(length=40), nullable=True),
        sa.Column("payer_ref", sa.String(length=40), nullable=True),
        sa.Column("attribution_label", sa.String(length=512), nullable=True),
        sa.Column("reimbursable", sa.Boolean(), nullable=True),
        sa.Column("reimbursable_amount", sa.String(length=48), nullable=True),
        sa.Column("receipt_ref", sa.String(length=40), nullable=True),
        sa.Column("receipt_status", sa.String(length=32), nullable=False),
        sa.Column("receipt_required", sa.Boolean(), nullable=False),
        sa.Column("receipt_note", sa.String(length=1000), nullable=True),
        sa.Column("ownership", sa.String(length=32), nullable=False),
        sa.Column("owner_ref", sa.String(length=40), nullable=True),
        sa.Column("source_method", sa.String(length=32), nullable=False),
        sa.Column("uncertainty", sa.String(length=1000), nullable=True),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ownership IN ('unknown','operator','organization','shared')",
            name="ck_expense_ownership",
        ),
        sa.CheckConstraint(
            "receipt_status IN ('not_provided','available','unavailable')",
            name="ck_expense_receipt_status",
        ),
        sa.CheckConstraint(
            "source_method IN ('report','import','inference')", name="ck_expense_source_method"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_expense_revision"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["engagement_ref"], ["engagements.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["expense_ref"], ["expenses.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_ref"], ["entities.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_ref"], ["entities.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payer_ref"], ["entities.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_ref"], ["entities.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receipt_ref"], ["sources.ref_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("expense_ref", "revision", name="uq_expense_revision"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_expense_revision_currency_date",
        "expense_revisions",
        ["currency", "incurred_on"],
        unique=False,
    )
    op.create_index(
        "ix_expense_revision_engagement_date",
        "expense_revisions",
        ["engagement_ref", "incurred_on"],
        unique=False,
    )
    op.create_table(
        "expense_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("expense_ref", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("basis_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.String(length=48), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('submitted','approved','rejected')", name="ck_expense_observation_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_expense_observation_version"),
        sa.ForeignKeyConstraint(
            ["basis_revision_ref"], ["expense_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["expense_ref"], ["expenses.ref_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("expense_ref", "version", name="uq_expense_observation_version"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_expense_observation_status",
        "expense_observations",
        ["expense_ref", "status", "version"],
        unique=False,
    )
    op.create_table(
        "expense_report_members",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("expense_ref", sa.String(length=40), nullable=False),
        sa.Column("revision_ref", sa.String(length=40), nullable=False),
        sa.Column("report_observation_ref", sa.String(length=40), nullable=True),
        sa.Column("approval_observation_ref", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(
            ["approval_observation_ref"], ["expense_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["expense_ref"], ["expenses.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["report_id"], ["expense_report_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["report_observation_ref"], ["expense_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revision_ref"], ["expense_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("report_id", "expense_ref"),
    )
    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()


def _postgres_guards() -> None:
    op.execute("""CREATE FUNCTION docket_expense_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Expense history is immutable'; END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.version <> 1 THEN RAISE EXCEPTION 'Expense must start at revision one'; END IF;
        ELSE
            IF NEW.version <> OLD.version + 1 OR
               (to_jsonb(NEW) - ARRAY['version','retracted','updated_at']) <>
               (to_jsonb(OLD) - ARRAY['version','retracted','updated_at']) THEN
                RAISE EXCEPTION 'Expense ownership and external identity are immutable';
            END IF;
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_expense_head_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM expense_revisions WHERE expense_ref = NEW.ref_id
            AND revision = NEW.version AND retracted = NEW.retracted) THEN
            RAISE EXCEPTION 'Expense requires its exact retained revision';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute("""CREATE FUNCTION docket_expense_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE head expenses;
    BEGIN
        SELECT * INTO head FROM expenses WHERE ref_id = NEW.expense_ref FOR UPDATE;
        IF head.version IS DISTINCT FROM NEW.revision OR head.retracted <> NEW.retracted THEN
            RAISE EXCEPTION 'Expense revision must match its head';
        END IF;
        IF NEW.amount !~ '^(0|[1-9][0-9]{0,23})([.][0-9]{1,9})?$' OR
           NEW.currency !~ '^[A-Z]{3}$' OR
           (NEW.reimbursable_amount IS NOT NULL AND (
             NEW.reimbursable_amount !~ '^(0|[1-9][0-9]{0,23})([.][0-9]{1,9})?$' OR
             NEW.reimbursable_amount::numeric > NEW.amount::numeric OR
             (NEW.reimbursable = FALSE AND NEW.reimbursable_amount::numeric > 0))) THEN
            RAISE EXCEPTION 'Expense requires exact nonnegative amounts and currency';
        END IF;
        IF NEW.engagement_ref IS NOT NULL AND NOT EXISTS (SELECT 1 FROM engagements
            WHERE ref_id = NEW.engagement_ref AND operator_ref = head.operator_ref) THEN
            RAISE EXCEPTION 'Expense requires owned engagement';
        END IF;
        IF (NEW.receipt_status = 'available') <> (NEW.receipt_ref IS NOT NULL) THEN
            RAISE EXCEPTION 'Receipt availability must reflect retained source';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_expense_observation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE basis expense_revisions;
    BEGIN
        SELECT * INTO basis FROM expense_revisions WHERE ref_id = NEW.basis_revision_ref;
        IF basis.expense_ref IS DISTINCT FROM NEW.expense_ref OR basis.revision <> NEW.version OR
           basis.created_by_changeset_ref <> NEW.created_by_changeset_ref OR
           basis.currency <> NEW.currency OR (NEW.amount IS NOT NULL AND (
               NEW.amount !~ '^(0|[1-9][0-9]{0,23})([.][0-9]{1,9})?$' OR
               NEW.amount::numeric > basis.amount::numeric)) THEN
            RAISE EXCEPTION 'Expense observation requires its exact retained monetary basis';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_expense_report_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.sealed_at IS NOT NULL OR COALESCE(NEW.totals_json::jsonb, 'null') <> 'null'::jsonb THEN
                RAISE EXCEPTION 'Expense report must capture before sealing';
            END IF;
        ELSIF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Expense report is immutable';
        ELSIF OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR
            COALESCE(NEW.totals_json::jsonb, 'null') = 'null'::jsonb OR
            (to_jsonb(NEW) - ARRAY['sealed_at','totals_json']) <>
            (to_jsonb(OLD) - ARRAY['sealed_at','totals_json']) THEN
            RAISE EXCEPTION 'Expense report permits one seal only';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_expense_report_member_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE report expense_report_snapshots;
    BEGIN
        SELECT * INTO report FROM expense_report_snapshots WHERE id = NEW.report_id FOR UPDATE;
        IF report.sealed_at IS NOT NULL OR NOT EXISTS (SELECT 1 FROM expense_revisions r
            JOIN expenses e ON e.ref_id = r.expense_ref WHERE r.ref_id = NEW.revision_ref
            AND r.expense_ref = NEW.expense_ref AND e.operator_ref = report.operator_ref
            AND r.created_at <= report.as_of) THEN
            RAISE EXCEPTION 'Expense report member requires unsealed owned retained basis';
        END IF;
        IF NEW.report_observation_ref IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM expense_observations WHERE ref_id = NEW.report_observation_ref
            AND expense_ref = NEW.expense_ref AND created_at <= report.as_of) THEN
            RAISE EXCEPTION 'Expense report status must belong to its expense';
        END IF;
        IF NEW.approval_observation_ref IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM expense_observations WHERE ref_id = NEW.approval_observation_ref
            AND expense_ref = NEW.expense_ref AND status = 'approved' AND created_at <= report.as_of) THEN
            RAISE EXCEPTION 'Expense report approval must belong to its expense';
        END IF;
        RETURN NEW;
    END $$""")
    for table, function in (
        ("expenses", "docket_expense_head_guard"),
        ("expense_report_snapshots", "docket_expense_report_guard"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_expense_complete AFTER INSERT OR UPDATE ON expenses "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION docket_expense_head_complete()"
    )
    for table, function in (
        ("expense_revisions", "docket_expense_revision_insert"),
        ("expense_observations", "docket_expense_observation_insert"),
        ("expense_report_members", "docket_expense_report_member_insert"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_insert BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )


def downgrade() -> None:
    tables = (
        "expense_report_members",
        "expense_report_snapshots",
        "expense_observations",
        "expense_revisions",
        "expenses",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained expense history")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "docket_expense_head_guard",
            "docket_expense_head_complete",
            "docket_expense_revision_insert",
            "docket_expense_observation_insert",
            "docket_expense_report_guard",
            "docket_expense_report_member_insert",
        ):
            op.execute(f"DROP FUNCTION {function}()")

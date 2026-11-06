"""retain effective compensation and reporting policy timelines

Revision ID: 20261105e5f6
Revises: 20261105d4e5
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105e5f6"
down_revision = "20261105d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compensation_policies",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column(
            "engagement_ref",
            sa.String(40),
            sa.ForeignKey("engagements.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("component_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('pay', 'reporting')", name="ck_compensation_policy_kind"),
        sa.CheckConstraint("version >= 1", name="ck_compensation_policy_version"),
        sa.CheckConstraint(
            "kind <> 'reporting' OR component_key = 'base'", name="ck_compensation_reporting_key"
        ),
        sa.UniqueConstraint("ref_id", name=None),
        sa.UniqueConstraint(
            "engagement_ref", "kind", "component_key", name="uq_compensation_policy_stream"
        ),
    )
    op.create_index(
        "ix_compensation_policies_operator_ref",
        "compensation_policies",
        ["operator_ref"],
        unique=False,
    )
    op.create_table(
        "compensation_policy_revisions",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.Column(
            "policy_ref",
            sa.String(40),
            sa.ForeignKey("compensation_policies.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sealed", sa.Boolean(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_compensation_policy_revision"),
        sa.UniqueConstraint("ref_id", name=None),
        sa.UniqueConstraint("policy_ref", "revision", name="uq_compensation_policy_revision"),
    )
    op.create_table(
        "compensation_terms",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.Column(
            "policy_ref",
            sa.String(40),
            sa.ForeignKey("compensation_policies.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "derived_from_ref",
            sa.String(40),
            sa.ForeignKey("compensation_terms.ref_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("basis", sa.String(32), nullable=True),
        sa.Column("rate", sa.String(48), nullable=True),
        sa.Column("amount", sa.String(48), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column(
            "payer_ref",
            sa.String(40),
            sa.ForeignKey("entities.ref_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("compensable", sa.Boolean(), nullable=True),
        sa.Column("billable", sa.Boolean(), nullable=True),
        sa.Column("money_decimals", sa.Integer(), nullable=True),
        sa.Column("week_starts_on", sa.Integer(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("anchor_date", sa.Date(), nullable=True),
        sa.Column("rounding_minutes", sa.String(48), nullable=True),
        sa.Column("rounding_scope", sa.String(32), nullable=True),
        sa.Column("rounding_mode", sa.String(32), nullable=True),
        sa.Column("hour_limit_week", sa.String(48), nullable=True),
        sa.Column("overtime_after_hours", sa.String(48), nullable=True),
        sa.Column("overtime_multiplier", sa.String(48), nullable=True),
        sa.Column("overtime_scope", sa.String(32), nullable=True),
        sa.Column("group_with_payer", sa.Boolean(), nullable=True),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "basis IS NULL OR basis IN ('unknown', 'hourly', 'salary', 'fixed_fee', 'unpaid')",
            name="ck_compensation_basis",
        ),
        sa.CheckConstraint("rate IS NULL OR basis = 'hourly'", name="ck_compensation_hourly_rate"),
        sa.CheckConstraint(
            "money_decimals IS NULL OR (money_decimals >= 0 AND money_decimals <= 9)",
            name="ck_compensation_money_precision",
        ),
        sa.CheckConstraint(
            "amount IS NULL OR basis IN ('salary', 'fixed_fee')",
            name="ck_compensation_nonhourly_amount",
        ),
        sa.CheckConstraint(
            "(overtime_after_hours IS NULL) = (overtime_multiplier IS NULL)",
            name="ck_compensation_overtime_pair",
        ),
        sa.CheckConstraint(
            "overtime_scope IS NULL OR overtime_scope IN ('day', 'week')",
            name="ck_compensation_overtime_scope",
        ),
        sa.CheckConstraint(
            "period_days IS NULL OR (period_days >= 1 AND period_days <= 366)",
            name="ck_compensation_period",
        ),
        sa.CheckConstraint(
            "(period_days IS NULL) = (anchor_date IS NULL)", name="ck_compensation_period_anchor"
        ),
        sa.CheckConstraint(
            "rounding_mode IS NULL OR rounding_mode IN ('nearest', 'up', 'down')",
            name="ck_compensation_rounding_mode",
        ),
        sa.CheckConstraint(
            "rounding_scope IS NULL OR rounding_scope IN ('segment', 'session', 'day', 'period')",
            name="ck_compensation_rounding_scope",
        ),
        sa.CheckConstraint(
            "(kind = 'pay' AND basis IS NOT NULL AND money_decimals IS NOT NULL AND week_starts_on IS NULL AND period_days IS NULL AND anchor_date IS NULL AND rounding_minutes IS NULL AND rounding_scope IS NULL AND rounding_mode IS NULL AND hour_limit_week IS NULL AND overtime_after_hours IS NULL AND overtime_multiplier IS NULL AND overtime_scope IS NULL AND group_with_payer IS NULL) OR (kind = 'reporting' AND basis IS NULL AND rate IS NULL AND amount IS NULL AND currency IS NULL AND payer_ref IS NULL AND compensable IS NULL AND billable IS NULL AND money_decimals IS NULL AND week_starts_on IS NOT NULL AND rounding_scope IS NOT NULL AND rounding_mode IS NOT NULL AND overtime_scope IS NOT NULL AND group_with_payer IS NOT NULL)",
            name="ck_compensation_term_domain",
        ),
        sa.CheckConstraint("kind IN ('pay', 'reporting')", name="ck_compensation_term_kind"),
        sa.CheckConstraint(
            "end_at IS NULL OR end_at > start_at", name="ck_compensation_term_window"
        ),
        sa.CheckConstraint(
            "week_starts_on IS NULL OR (week_starts_on >= 0 AND week_starts_on <= 6)",
            name="ck_compensation_week_start",
        ),
        sa.UniqueConstraint("ref_id", name=None),
    )
    op.create_index(
        "ix_compensation_term_window",
        "compensation_terms",
        ["policy_ref", "start_at", "end_at"],
        unique=False,
    )
    op.create_table(
        "compensation_revision_terms",
        sa.Column(
            "revision_ref",
            sa.String(40),
            sa.ForeignKey("compensation_policy_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False, primary_key=True),
        sa.Column(
            "term_ref",
            sa.String(40),
            sa.ForeignKey("compensation_terms.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_compensation_term_order"),
        sa.UniqueConstraint("revision_ref", "term_ref", name="uq_compensation_revision_term"),
    )

    if op.get_bind().dialect.name == "postgresql":
        for column in (
            "rate",
            "amount",
            "rounding_minutes",
            "hour_limit_week",
            "overtime_after_hours",
            "overtime_multiplier",
        ):
            op.create_check_constraint(
                f"ck_compensation_decimal_{column}",
                "compensation_terms",
                f"{column} IS NULL OR ({column} ~ '^[0-9]+([.][0-9]+)?$' AND CAST({column} AS NUMERIC) >= 0)",
            )
        op.create_check_constraint(
            "ck_compensation_currency",
            "compensation_terms",
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
        )
        op.create_check_constraint(
            "ck_compensation_positive_rounding",
            "compensation_terms",
            "rounding_minutes IS NULL OR (CAST(rounding_minutes AS NUMERIC) > 0 AND CAST(rounding_minutes AS NUMERIC) <= 10080 AND mod(CAST(rounding_minutes AS NUMERIC) * 60000000, 1) = 0)",
        )
        for column in ("hour_limit_week", "overtime_after_hours"):
            op.create_check_constraint(
                f"ck_compensation_positive_{column}",
                "compensation_terms",
                f"{column} IS NULL OR (CAST({column} AS NUMERIC) > 0 AND CAST({column} AS NUMERIC) <= 168)",
            )
        op.create_check_constraint(
            "ck_compensation_multiplier",
            "compensation_terms",
            "overtime_multiplier IS NULL OR (CAST(overtime_multiplier AS NUMERIC) >= 1 AND CAST(overtime_multiplier AS NUMERIC) <= 100)",
        )
        op.execute("""CREATE FUNCTION docket_compensation_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE owner_ref text;
            BEGIN
                IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Compensation heads are append-only'; END IF;
                IF TG_OP = 'INSERT' THEN
                    SELECT operator_ref INTO owner_ref FROM engagements WHERE ref_id = NEW.engagement_ref;
                    IF owner_ref IS DISTINCT FROM NEW.operator_ref THEN RAISE EXCEPTION 'Compensation owner does not match engagement'; END IF;
                ELSIF NEW.version <> OLD.version + 1 OR
                      (to_jsonb(NEW) - ARRAY['version','updated_at']) <> (to_jsonb(OLD) - ARRAY['version','updated_at']) THEN
                    RAISE EXCEPTION 'Compensation head identity is immutable';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION docket_compensation_head_complete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM compensation_policy_revisions WHERE policy_ref = NEW.ref_id AND revision = NEW.version AND sealed) THEN
                    RAISE EXCEPTION 'Compensation head requires a sealed revision';
                END IF;
                RETURN NULL;
            END $$""")
        op.execute("""CREATE FUNCTION docket_compensation_term_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE policy_kind text; parent compensation_terms%ROWTYPE;
            BEGIN
                SELECT kind INTO policy_kind FROM compensation_policies WHERE ref_id = NEW.policy_ref;
                IF policy_kind IS DISTINCT FROM NEW.kind THEN RAISE EXCEPTION 'Compensation term kind does not match policy'; END IF;
                IF NEW.derived_from_ref IS NOT NULL THEN
                    SELECT * INTO parent FROM compensation_terms WHERE ref_id = NEW.derived_from_ref;
                    IF parent.policy_ref IS DISTINCT FROM NEW.policy_ref OR NEW.start_at < parent.start_at
                       OR (parent.end_at IS NOT NULL AND (NEW.end_at IS NULL OR NEW.end_at > parent.end_at))
                       OR (to_jsonb(NEW) - ARRAY['id','ref_id','start_at','end_at','derived_from_ref','created_by_changeset_ref','created_at']) <>
                          (to_jsonb(parent) - ARRAY['id','ref_id','start_at','end_at','derived_from_ref','created_by_changeset_ref','created_at']) THEN
                        RAISE EXCEPTION 'Derived compensation terms must preserve source values and narrow its window';
                    END IF;
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION docket_compensation_link_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE policy text; item compensation_terms%ROWTYPE;
            BEGIN
                SELECT policy_ref INTO policy FROM compensation_policy_revisions WHERE ref_id = NEW.revision_ref AND NOT sealed FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Compensation revision is sealed'; END IF;
                SELECT * INTO item FROM compensation_terms WHERE ref_id = NEW.term_ref;
                IF item.policy_ref IS DISTINCT FROM policy THEN RAISE EXCEPTION 'Compensation term belongs to another policy'; END IF;
                IF EXISTS (SELECT 1 FROM compensation_revision_terms l JOIN compensation_terms t ON t.ref_id = l.term_ref
                    WHERE l.revision_ref = NEW.revision_ref AND t.start_at < coalesce(item.end_at, 'infinity'::timestamptz)
                    AND coalesce(t.end_at, 'infinity'::timestamptz) > item.start_at) THEN
                    RAISE EXCEPTION 'Compensation timeline windows overlap';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION docket_compensation_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE total integer; first_ordinal integer; last_ordinal integer;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.sealed THEN RAISE EXCEPTION 'Compensation revision must be assembled before sealing'; END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'DELETE' OR OLD.sealed OR NOT NEW.sealed
                   OR (to_jsonb(NEW) - 'sealed') <> (to_jsonb(OLD) - 'sealed') THEN
                    RAISE EXCEPTION 'Compensation revision is append-only';
                END IF;
                SELECT count(*), min(ordinal), max(ordinal) INTO total, first_ordinal, last_ordinal
                    FROM compensation_revision_terms WHERE revision_ref = NEW.ref_id;
                IF total = 0 OR first_ordinal <> 0 OR last_ordinal <> total - 1 THEN
                    RAISE EXCEPTION 'Compensation revision requires an ordered timeline';
                END IF;
                IF EXISTS (SELECT 1 FROM (
                    SELECT t.start_at, lag(t.start_at) OVER (ORDER BY l.ordinal) AS previous_start
                    FROM compensation_revision_terms l JOIN compensation_terms t ON t.ref_id = l.term_ref
                    WHERE l.revision_ref = NEW.ref_id
                ) ordered WHERE previous_start >= start_at) THEN
                    RAISE EXCEPTION 'Compensation timeline must follow effective order';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute(
            "CREATE TRIGGER trg_compensation_head_guard BEFORE INSERT OR UPDATE OR DELETE ON compensation_policies FOR EACH ROW EXECUTE FUNCTION docket_compensation_head_guard()"
        )
        op.execute(
            "CREATE CONSTRAINT TRIGGER trg_compensation_head_complete AFTER INSERT OR UPDATE ON compensation_policies DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION docket_compensation_head_complete()"
        )
        op.execute(
            "CREATE TRIGGER trg_compensation_revision_guard BEFORE INSERT OR UPDATE OR DELETE ON compensation_policy_revisions FOR EACH ROW EXECUTE FUNCTION docket_compensation_revision_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_compensation_term_insert BEFORE INSERT ON compensation_terms FOR EACH ROW EXECUTE FUNCTION docket_compensation_term_insert()"
        )
        op.execute(
            "CREATE TRIGGER trg_compensation_link_insert BEFORE INSERT ON compensation_revision_terms FOR EACH ROW EXECUTE FUNCTION docket_compensation_link_insert()"
        )
        for table in ("compensation_terms", "compensation_revision_terms"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
            )


def downgrade() -> None:
    tables = (
        "compensation_revision_terms",
        "compensation_terms",
        "compensation_policy_revisions",
        "compensation_policies",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained compensation policy history")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "docket_compensation_revision_guard",
            "docket_compensation_link_insert",
            "docket_compensation_term_insert",
            "docket_compensation_head_complete",
            "docket_compensation_head_guard",
        ):
            op.execute(f"DROP FUNCTION {function}()")

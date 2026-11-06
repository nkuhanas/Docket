"""retain formal financial documents and conserved work claims

Revision ID: 20261105a7b8
Revises: 20261105f6a7
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105a7b8"
down_revision = "20261105f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("engagement_ref", sa.String(length=40), nullable=False),
        sa.Column("component_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('timesheet', 'invoice')", name="ck_financial_document_kind"),
        sa.CheckConstraint(
            "status IN ('draft', 'verified', 'submitted', 'approved', 'rejected', 'issued', 'cancelled')",
            name="ck_financial_document_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_financial_document_version"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["engagement_ref"], ["engagements.ref_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        op.f("ix_financial_documents_operator_ref"),
        "financial_documents",
        ["operator_ref"],
        unique=False,
    )
    op.create_table(
        "financial_document_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("document_ref", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_ref", sa.String(length=40), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("observation", sa.Text(), nullable=True),
        sa.Column("observation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("amends_ref", sa.String(length=40), nullable=True),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'verified', 'submitted', 'approved', 'rejected', 'issued', 'cancelled')",
            name="ck_financial_revision_status",
        ),
        sa.CheckConstraint("date_to > date_from", name="ck_financial_document_period"),
        sa.CheckConstraint("revision >= 1", name="ck_financial_document_revision"),
        sa.ForeignKeyConstraint(
            ["amends_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"], ["compensation_evaluations.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_ref", "revision", name="uq_financial_document_revision"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_table(
        "work_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("origin_ref", sa.String(length=40), nullable=False),
        sa.Column("component_key", sa.String(length=64), nullable=False),
        sa.Column("engagement_ref", sa.String(length=40), nullable=False),
        sa.Column("start_us", sa.BigInteger(), nullable=False),
        sa.Column("end_us", sa.BigInteger(), nullable=False),
        sa.Column("source_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("evaluation_ref", sa.String(length=40), nullable=False),
        sa.Column("evaluation_line", sa.Integer(), nullable=False),
        sa.Column("document_ref", sa.String(length=40), nullable=False),
        sa.Column("reportable_us", sa.BigInteger(), nullable=True),
        sa.Column("pay_term_ref", sa.String(length=40), nullable=True),
        sa.Column("gross_numerator", sa.String(length=128), nullable=True),
        sa.Column("gross_denominator", sa.String(length=128), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'reported', 'issued', 'released')", name="ck_work_claim_status"
        ),
        sa.CheckConstraint(
            "(gross_numerator IS NULL) = (gross_denominator IS NULL)",
            name="ck_work_claim_money_pair",
        ),
        sa.CheckConstraint("evaluation_line >= 0", name="ck_work_claim_evaluation_line"),
        sa.CheckConstraint(
            "reportable_us IS NULL OR reportable_us >= 0", name="ck_work_claim_reportable"
        ),
        sa.CheckConstraint("start_us >= 0 AND end_us > start_us", name="ck_work_claim_bounds"),
        sa.CheckConstraint("version >= 1", name="ck_work_claim_version"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["engagement_ref"], ["engagements.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"], ["compensation_evaluations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["origin_ref"], ["work_quantity_origins.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["pay_term_ref"], ["compensation_terms.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_ref"], ["work_session_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_work_claim_availability",
        "work_claims",
        ["origin_ref", "component_key", "start_us", "end_us"],
        unique=False,
    )
    op.create_table(
        "work_claim_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_ref", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("document_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'reported', 'issued', 'released')",
            name="ck_work_claim_event_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_work_claim_event_version"),
        sa.ForeignKeyConstraint(["claim_ref"], ["work_claims.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_ref"], ["financial_document_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_ref", "version", name="uq_work_claim_event"),
    )
    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()


def _postgres_guards() -> None:
    op.execute("""CREATE FUNCTION docket_financial_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Financial document history is immutable'; END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.version <> 1 OR NEW.status <> 'draft' OR NOT EXISTS (
                SELECT 1 FROM engagements WHERE ref_id = NEW.engagement_ref AND operator_ref = NEW.operator_ref
            ) THEN RAISE EXCEPTION 'Financial document requires owned engagement and initial draft'; END IF;
        ELSE
            IF NEW.version <> OLD.version + 1 OR
                (to_jsonb(NEW) - ARRAY['version','status','updated_at']) <>
                (to_jsonb(OLD) - ARRAY['version','status','updated_at']) THEN
                RAISE EXCEPTION 'Financial document identity is immutable';
            END IF;
            IF NOT (
                (OLD.status = 'draft' AND NEW.status IN ('draft','verified','submitted','issued','cancelled')) OR
                (OLD.status = 'verified' AND NEW.status IN ('draft','verified','submitted','cancelled')) OR
                (OLD.status = 'submitted' AND NEW.status IN ('approved','rejected'))
            ) THEN RAISE EXCEPTION 'Invalid financial document transition'; END IF;
        END IF;
        IF (NEW.kind = 'timesheet' AND NEW.status = 'issued') OR
           (NEW.kind = 'invoice' AND NEW.status NOT IN ('draft','issued','cancelled')) THEN
            RAISE EXCEPTION 'Financial document state must match its kind';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_financial_head_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM financial_document_revisions
            WHERE document_ref = NEW.ref_id AND revision = NEW.version AND status = NEW.status) THEN
            RAISE EXCEPTION 'Financial document requires retained revision';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute("""CREATE FUNCTION docket_financial_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE head financial_documents; prior financial_document_revisions;
    BEGIN
        SELECT * INTO head FROM financial_documents WHERE ref_id = NEW.document_ref FOR UPDATE;
        IF head.version <> NEW.revision OR head.status <> NEW.status OR NOT EXISTS (
            SELECT 1 FROM compensation_evaluations WHERE ref_id = NEW.evaluation_ref
                AND operator_ref = head.operator_ref
                AND parameters_json->>'engagement_ref' = head.engagement_ref
                AND parameters_json->>'component_key' = head.component_key
        ) THEN RAISE EXCEPTION 'Financial revision must retain owned matching calculation'; END IF;
        SELECT * INTO prior FROM financial_document_revisions
            WHERE document_ref = NEW.document_ref AND revision = NEW.revision - 1;
        IF prior.status IN ('submitted','approved','rejected','issued') AND (
            to_jsonb(NEW.snapshot_json) <> to_jsonb(prior.snapshot_json) OR
            NEW.evaluation_ref <> prior.evaluation_ref OR NEW.date_from <> prior.date_from OR
            NEW.date_to <> prior.date_to OR NEW.timezone <> prior.timezone
        ) THEN RAISE EXCEPTION 'Submitted or issued snapshot is immutable'; END IF;
        IF NEW.amends_ref IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM financial_documents WHERE ref_id = NEW.amends_ref
                AND operator_ref = head.operator_ref AND engagement_ref = head.engagement_ref
                AND kind = head.kind AND ref_id <> head.ref_id
        ) THEN RAISE EXCEPTION 'Amendments require an owned related document'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_work_claim_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner text;
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Work claim history is immutable'; END IF;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.version <> OLD.version + 1 OR
               (to_jsonb(NEW) - ARRAY['version','status']) <> (to_jsonb(OLD) - ARRAY['version','status']) OR
               NOT (OLD.status = 'reserved' AND NEW.status IN ('reported','issued','released') OR
                    OLD.status = 'reported' AND NEW.status = 'issued') THEN
                RAISE EXCEPTION 'Work claim quantity and monetary basis are immutable';
            END IF;
            RETURN NEW;
        END IF;
        SELECT operator_ref INTO owner FROM work_quantity_origins WHERE ref_id = NEW.origin_ref FOR UPDATE;
        IF owner IS DISTINCT FROM NEW.operator_ref OR NEW.version <> 1 OR NEW.status <> 'reserved' THEN
            RAISE EXCEPTION 'Work claim requires owned quantity and initial reservation';
        END IF;
        IF NEW.component_key !~ '^[a-z][a-z0-9_]{0,63}$' OR
           (NEW.currency IS NOT NULL AND NEW.currency !~ '^[A-Z]{3}$') OR
           (NEW.gross_numerator IS NOT NULL AND (NEW.gross_numerator !~ '^[0-9]+$' OR
                NEW.gross_denominator !~ '^[1-9][0-9]*$')) THEN
            RAISE EXCEPTION 'Work claim monetary quantities require exact nonnegative fractions';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM financial_documents d JOIN compensation_evaluations e
            ON e.ref_id = NEW.evaluation_ref JOIN financial_document_revisions r
            ON r.document_ref = d.ref_id AND r.revision = d.version
            WHERE d.ref_id = NEW.document_ref AND d.status = 'draft'
            AND r.evaluation_ref = NEW.evaluation_ref
            AND (r.snapshot_json::jsonb->'claim_refs') ? NEW.ref_id
            AND d.operator_ref = NEW.operator_ref AND d.engagement_ref = NEW.engagement_ref
            AND d.component_key = NEW.component_key AND e.operator_ref = NEW.operator_ref
            AND e.result_json->'contributions'->NEW.evaluation_line->>'revision_ref' = NEW.source_revision_ref
        ) THEN RAISE EXCEPTION 'Work claim requires a matching document and contribution'; END IF;
        IF NOT EXISTS (SELECT 1 FROM work_quantity_allocations a JOIN work_quantity_bases b
            ON b.revision_ref = a.revision_ref WHERE a.revision_ref = NEW.source_revision_ref
            AND b.sealed AND a.origin_ref = NEW.origin_ref
            AND a.start_us <= NEW.start_us AND a.end_us >= NEW.end_us
        ) THEN RAISE EXCEPTION 'Work claim exceeds retained source quantity'; END IF;
        IF EXISTS (SELECT 1 FROM work_claims WHERE origin_ref = NEW.origin_ref
            AND component_key = NEW.component_key AND status <> 'released'
            AND start_us < NEW.end_us AND end_us > NEW.start_us
        ) THEN RAISE EXCEPTION 'Work quantity already reserved or claimed for this component'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_work_claim_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM work_claim_events WHERE claim_ref = NEW.ref_id
            AND version = NEW.version AND status = NEW.status) THEN
            RAISE EXCEPTION 'Work claim requires retained transition evidence';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute("""CREATE FUNCTION docket_work_claim_event_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM work_claims c JOIN financial_document_revisions r
            ON r.ref_id = NEW.document_revision_ref WHERE c.ref_id = NEW.claim_ref
            AND c.version = NEW.version AND c.status = NEW.status
            AND r.document_ref = c.document_ref AND r.created_by_changeset_ref = NEW.created_by_changeset_ref
            AND ((NEW.status = 'reserved' AND r.status = 'draft') OR
                 (NEW.status = 'reported' AND r.status = 'submitted') OR
                 (NEW.status = 'issued' AND r.status = 'issued') OR
                 (NEW.status = 'released' AND r.status IN ('draft','cancelled')))
        ) THEN RAISE EXCEPTION 'Claim transition must match its current document revision'; END IF;
        RETURN NEW;
    END $$""")
    for table, function in (
        ("financial_documents", "docket_financial_head_guard"),
        ("work_claims", "docket_work_claim_guard"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    for table, function in (
        ("financial_documents", "docket_financial_head_complete"),
        ("work_claims", "docket_work_claim_complete"),
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER trg_{table}_complete AFTER INSERT OR UPDATE ON {table} "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    for table, function in (
        ("financial_document_revisions", "docket_financial_revision_insert"),
        ("work_claim_events", "docket_work_claim_event_insert"),
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
        "work_claim_events",
        "work_claims",
        "financial_document_revisions",
        "financial_documents",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained financial documents or claims")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "docket_work_claim_event_insert",
            "docket_work_claim_complete",
            "docket_work_claim_guard",
            "docket_financial_revision_insert",
            "docket_financial_head_complete",
            "docket_financial_head_guard",
        ):
            op.execute(f"DROP FUNCTION {function}()")

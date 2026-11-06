"""retain source-backed invoice allocations and numbering

Revision ID: 20261105c9d0
Revises: 20261105b8c9
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105c9d0"
down_revision = "20261105b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_number_sequences",
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("series", sa.String(length=64), nullable=False),
        sa.Column("next_ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("next_ordinal >= 1", name="ck_invoice_sequence_ordinal"),
        sa.PrimaryKeyConstraint("operator_ref", "series"),
    )
    op.create_table(
        "invoice_numbers",
        sa.Column("document_ref", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("series", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(length=100), nullable=False),
        sa.Column("issued_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_invoice_ordinal"),
        sa.ForeignKeyConstraint(
            ["document_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["issued_revision_ref"], ["financial_document_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("document_ref"),
        sa.UniqueConstraint("operator_ref", "number", name="uq_invoice_number"),
        sa.UniqueConstraint("operator_ref", "series", "ordinal", name="uq_invoice_ordinal"),
    )
    op.create_table(
        "document_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("document_ref", sa.String(length=40), nullable=False),
        sa.Column("work_claim_ref", sa.String(length=40), nullable=True),
        sa.Column("expense_ref", sa.String(length=40), nullable=True),
        sa.Column("expense_revision_ref", sa.String(length=40), nullable=True),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("start_units", sa.String(length=48), nullable=False),
        sa.Column("end_units", sa.String(length=48), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("money_decimals", sa.Integer(), nullable=False),
        sa.Column("gross_numerator", sa.String(length=160), nullable=True),
        sa.Column("gross_denominator", sa.String(length=160), nullable=True),
        sa.Column("quote_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved','issued','released')", name="ck_document_allocation_status"
        ),
        sa.CheckConstraint(
            "(gross_numerator IS NULL) = (gross_denominator IS NULL)",
            name="ck_document_allocation_money",
        ),
        sa.CheckConstraint(
            "(work_claim_ref IS NOT NULL AND expense_ref IS NULL AND expense_revision_ref IS NULL) OR (work_claim_ref IS NULL AND expense_ref IS NOT NULL AND expense_revision_ref IS NOT NULL)",
            name="ck_document_allocation_source",
        ),
        sa.CheckConstraint(
            "money_decimals BETWEEN 0 AND 9", name="ck_document_allocation_precision"
        ),
        sa.CheckConstraint("version >= 1", name="ck_document_allocation_version"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["expense_ref"], ["expenses.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["expense_revision_ref"], ["expense_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["work_claim_ref"], ["work_claims.ref_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_document_allocation_document",
        "document_allocations",
        ["document_ref", "status"],
        unique=False,
    )
    op.create_index(
        "ix_document_allocation_source",
        "document_allocations",
        ["operator_ref", "source_key", "status"],
        unique=False,
    )
    op.create_table(
        "document_allocation_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("allocation_ref", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("document_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved','issued','released')", name="ck_document_allocation_event_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_document_allocation_event_version"),
        sa.ForeignKeyConstraint(
            ["allocation_ref"], ["document_allocations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_ref"], ["financial_document_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("allocation_ref", "version", name="uq_document_allocation_event"),
    )
    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()


def _postgres_guards() -> None:
    op.execute("""CREATE FUNCTION docket_document_allocation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE doc financial_documents; claim work_claims; expense expenses;
            basis expense_revisions; source_lower numeric; source_upper numeric;
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Document allocation history is immutable'; END IF;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.version <> OLD.version + 1 OR OLD.status <> 'reserved' OR
               NEW.status NOT IN ('issued','released') OR
               (to_jsonb(NEW) - ARRAY['version','status']) <> (to_jsonb(OLD) - ARRAY['version','status']) THEN
                RAISE EXCEPTION 'Document allocation quantities and quotes are immutable';
            END IF;
            RETURN NEW;
        END IF;
        SELECT * INTO doc FROM financial_documents WHERE ref_id = NEW.document_ref;
        IF doc.operator_ref IS DISTINCT FROM NEW.operator_ref OR doc.kind <> 'invoice' OR
           doc.status <> 'draft' OR NEW.status <> 'reserved' OR NEW.version <> 1 THEN
            RAISE EXCEPTION 'Allocation requires owned draft invoice';
        END IF;
        IF NEW.start_units !~ '^(0|[1-9][0-9]{0,47})$' OR NEW.end_units !~ '^[1-9][0-9]{0,47}$' OR
           NEW.end_units::numeric <= NEW.start_units::numeric OR
           (NEW.gross_numerator IS NOT NULL AND (NEW.gross_numerator !~ '^[0-9]+$' OR
             NEW.gross_denominator !~ '^[1-9][0-9]*$')) OR
           (NEW.currency IS NOT NULL AND NEW.currency !~ '^[A-Z]{3}$') THEN
            RAISE EXCEPTION 'Allocation requires exact nonnegative quantities and money';
        END IF;
        IF NEW.work_claim_ref IS NOT NULL THEN
            PERFORM o.ref_id FROM work_quantity_origins o JOIN work_claims c
                ON c.origin_ref = o.ref_id WHERE c.ref_id = NEW.work_claim_ref FOR UPDATE OF o;
            SELECT * INTO claim FROM work_claims WHERE ref_id = NEW.work_claim_ref FOR UPDATE;
            IF claim.operator_ref IS DISTINCT FROM NEW.operator_ref OR claim.status = 'released' OR
               (claim.status = 'reserved' AND claim.document_ref <> NEW.document_ref) OR
               claim.engagement_ref <> doc.engagement_ref OR
               claim.component_key <> doc.component_key OR
               NEW.source_key <> 'work:' || claim.origin_ref || ':' || claim.component_key OR
               NEW.quote_json->>'kind' IS DISTINCT FROM 'work' THEN
                RAISE EXCEPTION 'Allocation requires owned conserved work claim';
            END IF;
            source_lower := claim.start_us; source_upper := claim.end_us;
        ELSE
            SELECT * INTO expense FROM expenses WHERE ref_id = NEW.expense_ref FOR UPDATE;
            SELECT * INTO basis FROM expense_revisions WHERE ref_id = NEW.expense_revision_ref;
            IF expense.operator_ref IS DISTINCT FROM NEW.operator_ref OR expense.retracted OR
               basis.expense_ref IS DISTINCT FROM NEW.expense_ref OR expense.version <> basis.revision OR
               (basis.engagement_ref IS NOT NULL AND basis.engagement_ref <> doc.engagement_ref) OR
               NEW.currency IS DISTINCT FROM basis.currency OR
               NEW.source_key <> 'expense:' || expense.ref_id OR
               NEW.quote_json->>'kind' IS DISTINCT FROM 'expense' THEN
                RAISE EXCEPTION 'Allocation requires owned current expense basis and currency';
            END IF;
            source_lower := 0;
            source_upper := COALESCE(basis.reimbursable_amount, basis.amount)::numeric * 1000000000;
        END IF;
        IF NEW.start_units::numeric < source_lower OR NEW.end_units::numeric > source_upper THEN
            RAISE EXCEPTION 'Allocation exceeds retained source quantity';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM financial_document_revisions r WHERE r.document_ref = doc.ref_id
            AND r.revision = doc.version AND r.status = 'draft'
            AND (r.snapshot_json::jsonb->'allocation_refs') ? NEW.ref_id) THEN
            RAISE EXCEPTION 'Allocation requires exact draft revision membership';
        END IF;
        IF EXISTS (SELECT 1 FROM document_allocations WHERE operator_ref = NEW.operator_ref
            AND source_key = NEW.source_key AND status <> 'released'
            AND start_units::numeric < NEW.end_units::numeric
            AND end_units::numeric > NEW.start_units::numeric) THEN
            RAISE EXCEPTION 'Source quantity already allocated to an invoice';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_document_allocation_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM document_allocation_events WHERE allocation_ref = NEW.ref_id
            AND version = NEW.version AND status = NEW.status) THEN
            RAISE EXCEPTION 'Document allocation requires retained transition evidence';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute("""CREATE FUNCTION docket_document_allocation_event_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM document_allocations a JOIN financial_document_revisions r
            ON r.ref_id = NEW.document_revision_ref WHERE a.ref_id = NEW.allocation_ref
            AND a.version = NEW.version AND a.status = NEW.status AND r.document_ref = a.document_ref
            AND r.created_by_changeset_ref = NEW.created_by_changeset_ref AND (
                NEW.status = 'reserved' AND r.status = 'draft' OR
                NEW.status = 'issued' AND r.status = 'issued' OR
                NEW.status = 'released' AND r.status IN ('draft','cancelled'))) THEN
            RAISE EXCEPTION 'Allocation transition requires its exact document revision';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_invoice_number_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM financial_documents d JOIN financial_document_revisions r
            ON r.document_ref = d.ref_id WHERE d.ref_id = NEW.document_ref AND d.kind = 'invoice'
            AND d.operator_ref = NEW.operator_ref AND d.status = 'issued' AND r.status = 'issued'
            AND r.ref_id = NEW.issued_revision_ref AND r.revision = d.version) THEN
            RAISE EXCEPTION 'Invoice number requires its exact issued owned revision';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_invoice_sequence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Invoice number sequence is immutable'; END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.next_ordinal <> 1 THEN RAISE EXCEPTION 'Invoice series must start at one'; END IF;
        ELSIF NEW.next_ordinal <> OLD.next_ordinal + 1 OR NEW.operator_ref <> OLD.operator_ref OR
              NEW.series <> OLD.series THEN
            RAISE EXCEPTION 'Invoice number sequence permits one forward allocation';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION docket_invoice_issue_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body financial_document_revisions;
    BEGIN
        IF NEW.kind <> 'invoice' OR NEW.status <> 'issued' THEN RETURN NULL; END IF;
        SELECT * INTO body FROM financial_document_revisions
            WHERE document_ref = NEW.ref_id AND revision = NEW.version;
        IF NOT EXISTS (SELECT 1 FROM invoice_numbers WHERE document_ref = NEW.ref_id
            AND issued_revision_ref = body.ref_id) THEN
            RAISE EXCEPTION 'Issued invoice requires retained unique number';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM document_allocations WHERE document_ref = NEW.ref_id AND status = 'issued') OR
           EXISTS (SELECT 1 FROM document_allocations WHERE document_ref = NEW.ref_id AND status <> 'released'
            AND (status <> 'issued' OR gross_numerator IS NULL OR currency IS NULL OR
                 NOT ((body.snapshot_json::jsonb->'allocation_refs') ? ref_id))) OR
           (SELECT COUNT(DISTINCT currency) FROM document_allocations
            WHERE document_ref = NEW.ref_id AND status = 'issued') <> 1 THEN
            RAISE EXCEPTION 'Issued invoice requires complete single-currency priced allocations';
        END IF;
        IF EXISTS (SELECT 1 FROM document_allocations a JOIN expense_revisions r
            ON r.ref_id = a.expense_revision_ref JOIN expenses e ON e.ref_id = r.expense_ref
            WHERE a.document_ref = NEW.ref_id AND a.status = 'issued' AND (
                e.version <> r.revision OR e.retracted OR r.reimbursable IS DISTINCT FROM TRUE OR
                r.receipt_required AND r.receipt_status <> 'available')) THEN
            RAISE EXCEPTION 'Affected expense claim requires current eligible basis and required receipt';
        END IF;
        IF EXISTS (SELECT 1 FROM document_allocations WHERE document_ref = NEW.ref_id
            AND status = 'issued' AND work_claim_ref IS NOT NULL
            AND quote_json->>'billable' IS DISTINCT FROM 'true') THEN
            RAISE EXCEPTION 'Issued labor requires evaluated billability';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_invoice_issue_complete AFTER INSERT OR UPDATE ON financial_documents "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION docket_invoice_issue_complete()"
    )
    op.execute(
        "CREATE TRIGGER trg_allocation_guard BEFORE INSERT OR UPDATE OR DELETE ON document_allocations "
        "FOR EACH ROW EXECUTE FUNCTION docket_document_allocation_guard()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_allocation_complete AFTER INSERT OR UPDATE ON document_allocations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION docket_document_allocation_complete()"
    )
    for table, function in (
        ("document_allocation_events", "docket_document_allocation_event_insert"),
        ("invoice_numbers", "docket_invoice_number_guard"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_insert BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
    op.execute(
        "CREATE TRIGGER trg_invoice_sequence BEFORE INSERT OR UPDATE OR DELETE ON invoice_number_sequences "
        "FOR EACH ROW EXECUTE FUNCTION docket_invoice_sequence_guard()"
    )


def downgrade() -> None:
    tables = (
        "document_allocation_events",
        "document_allocations",
        "invoice_numbers",
        "invoice_number_sequences",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained allocations or invoice numbering")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_invoice_issue_complete ON financial_documents")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "docket_invoice_issue_complete",
            "docket_document_allocation_guard",
            "docket_document_allocation_complete",
            "docket_document_allocation_event_insert",
            "docket_invoice_number_guard",
            "docket_invoice_sequence_guard",
        ):
            op.execute(f"DROP FUNCTION {function}()")

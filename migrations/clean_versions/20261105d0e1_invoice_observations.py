"""retain reported invoice delivery and finalization guards

Revision ID: 20261105d0e1
Revises: 20261105c9d0
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105d0e1"
down_revision = "20261105c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_document_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("document_ref", sa.String(length=40), nullable=False),
        sa.Column("document_revision_ref", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind = 'reported_delivery'", name="ck_financial_observation_kind"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_ref"], ["financial_documents.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_ref"], ["financial_document_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_financial_observation_document",
        "financial_document_observations",
        ["document_ref", "created_at"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()


def _postgres_guards() -> None:
    op.execute("""CREATE FUNCTION docket_financial_observation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM financial_documents d JOIN financial_document_revisions r
            ON r.document_ref = d.ref_id WHERE d.ref_id = NEW.document_ref AND d.kind = 'invoice'
            AND d.status = 'issued' AND r.ref_id = NEW.document_revision_ref AND r.status = 'issued'
            AND r.revision = d.version) THEN
            RAISE EXCEPTION 'Delivery observation requires its exact issued invoice';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER trg_financial_observation_insert BEFORE INSERT ON financial_document_observations "
        "FOR EACH ROW EXECUTE FUNCTION docket_financial_observation_insert()"
    )
    op.execute(
        "CREATE TRIGGER trg_financial_observation_immutable BEFORE UPDATE OR DELETE ON financial_document_observations "
        "FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
    )
    op.execute("""CREATE FUNCTION docket_invoice_reporting_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.kind <> 'invoice' OR NEW.status <> 'issued' THEN RETURN NULL; END IF;
        IF (SELECT COUNT(DISTINCT money_decimals) FROM document_allocations
            WHERE document_ref = NEW.ref_id AND status = 'issued') <> 1 THEN
            RAISE EXCEPTION 'Invoice requires one explicit monetary precision';
        END IF;
        IF EXISTS (SELECT 1 FROM document_allocations a JOIN expense_observations o
            ON o.expense_ref = a.expense_ref WHERE a.document_ref = NEW.ref_id AND a.status = 'issued'
            AND o.status = 'rejected' AND o.version = (SELECT MAX(version) FROM expense_observations
                WHERE expense_ref = a.expense_ref)) THEN
            RAISE EXCEPTION 'Rejected expense report requires reconciliation before invoicing';
        END IF;
        RETURN NULL;
    END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_invoice_reporting_complete AFTER INSERT OR UPDATE ON financial_documents "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION docket_invoice_reporting_complete()"
    )
    op.execute("""CREATE FUNCTION docket_expense_allocation_currency() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.expense_ref IS NULL THEN RETURN NEW; END IF;
        PERFORM ref_id FROM expenses WHERE ref_id = NEW.expense_ref FOR UPDATE;
        IF EXISTS (SELECT 1 FROM document_allocations WHERE expense_ref = NEW.expense_ref
            AND status <> 'released' AND currency IS DISTINCT FROM NEW.currency) THEN
            RAISE EXCEPTION 'Expense currency correction requires prior claim reconciliation';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER trg_expense_allocation_currency BEFORE INSERT ON document_allocations "
        "FOR EACH ROW EXECUTE FUNCTION docket_expense_allocation_currency()"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM financial_document_observations)"))
        .scalar()
    ):
        raise RuntimeError("Downgrade cannot discard retained financial delivery observations")
    op.drop_table("financial_document_observations")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_invoice_reporting_complete ON financial_documents")
        op.execute("DROP TRIGGER trg_expense_allocation_currency ON document_allocations")
        for function in (
            "docket_financial_observation_insert",
            "docket_invoice_reporting_complete",
            "docket_expense_allocation_currency",
        ):
            op.execute(f"DROP FUNCTION {function}()")

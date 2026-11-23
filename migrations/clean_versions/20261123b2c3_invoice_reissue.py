"""Allow numbered invoice revisions to replace their own posted allocations."""

import sqlalchemy as sa
from alembic import op

revision = "20261123b2c3"
down_revision = "20261123a1b2"
branch_labels = None
depends_on = None

# Amend only the relevant predicates in the installed financial guards. Keep the
# original immutable snapshots, source bounds, settlement checks and number rows.
GUARDS = {
    "docket_financial_head_guard": [
        (
            "(OLD.status = 'verified' AND",
            "(OLD.kind = 'invoice' AND OLD.status = 'issued' AND NEW.status = 'draft') OR\n                (OLD.status = 'verified' AND",
        ),
    ],
    "docket_document_allocation_guard": [
        (
            "OLD.status <> 'reserved' OR",
            """NOT (OLD.status = 'reserved' OR
                    (OLD.status = 'issued' AND NEW.status = 'released' AND EXISTS (
                        SELECT 1 FROM financial_documents d JOIN financial_document_revisions r
                        ON r.document_ref = d.ref_id AND r.revision = d.version
                        WHERE d.ref_id = NEW.document_ref AND r.status = 'issued'
                        AND d.status = 'issued' AND NOT ((r.snapshot_json::jsonb->'allocation_refs') ? NEW.ref_id)
                        AND EXISTS (SELECT 1 FROM financial_document_revisions p
                            WHERE p.document_ref = d.ref_id AND p.status = 'issued' AND p.revision < r.revision)
                    ))) OR""",
        ),
        (
            "AND source_key = NEW.source_key AND status <> 'released'",
            "AND source_key = NEW.source_key AND status <> 'released'\n            AND NOT (document_ref = NEW.document_ref AND status = 'issued')",
        ),
    ],
    "docket_document_allocation_event_insert": [
        ("r.status IN ('draft','cancelled')", "r.status IN ('draft','cancelled','issued')"),
    ],
    "docket_invoice_issue_complete": [
        (
            "AND issued_revision_ref = body.ref_id",
            """AND issued_revision_ref IN (
                SELECT r.ref_id FROM financial_document_revisions r
                WHERE r.document_ref = NEW.ref_id AND r.status = 'issued' AND r.revision <= body.revision)""",
        ),
    ],
}


def revise_guards(*, forward: bool) -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    for name, edits in GUARDS.items():
        ddl = connection.scalar(
            sa.text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
            {"signature": name + "()"},
        )
        for original, replacement in edits:
            before, after = (original, replacement) if forward else (replacement, original)
            if ddl is None or ddl.count(before) != 1:
                raise RuntimeError(f"Unrecognized definition for {name}")
            ddl = ddl.replace(before, after)
        connection.exec_driver_sql(ddl)


def upgrade() -> None:
    revise_guards(forward=True)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM financial_document_revisions a
          JOIN financial_document_revisions b ON b.document_ref = a.document_ref
          WHERE a.status = 'issued' AND b.revision > a.revision)
    """)
    ):
        raise RuntimeError("Retained invoice corrections prevent downgrade")
    revise_guards(forward=False)

"""retain stable quantity coordinates and work split/merge lineage

Revision ID: 20261105c3d4
Revises: 20261105b2c3
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105c3d4"
down_revision = "20261105b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_quantity_origins",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column(
            "source_work_ref",
            sa.String(40),
            sa.ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("next_offset_us", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("next_offset_us >= 0", name="ck_work_origin_offset"),
    )
    op.create_index(
        "ix_work_quantity_origins_operator_ref", "work_quantity_origins", ["operator_ref"]
    )
    op.create_table(
        "work_quantity_bases",
        sa.Column(
            "revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sealed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('resolved', 'unknown', 'review')", name="ck_work_basis_status"
        ),
    )
    op.create_table(
        "work_quantity_allocations",
        sa.Column(
            "revision_ref",
            sa.String(40),
            sa.ForeignKey("work_quantity_bases.revision_ref", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column(
            "origin_ref",
            sa.String(40),
            sa.ForeignKey("work_quantity_origins.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("start_us", sa.BigInteger(), nullable=False),
        sa.Column("end_us", sa.BigInteger(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.CheckConstraint("start_us >= 0 AND end_us > start_us", name="ck_work_allocation_bounds"),
        sa.CheckConstraint("ordinal >= 0", name="ck_work_allocation_order"),
    )
    op.create_index(
        "ix_work_allocation_origin",
        "work_quantity_allocations",
        ["origin_ref", "start_us", "end_us"],
    )
    op.create_table(
        "work_lineage_edges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "parent_revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "child_revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.Column(
            "created_by_changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("relation IN ('split', 'merge')", name="ck_work_lineage_relation"),
        sa.UniqueConstraint(
            "parent_revision_ref", "child_revision_ref", name="uq_work_lineage_edge"
        ),
    )
    op.create_index("ix_work_lineage_child", "work_lineage_edges", ["child_revision_ref"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION docket_work_origin_growth() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' OR NEW.next_offset_us < OLD.next_offset_us
                   OR (to_jsonb(NEW) - 'next_offset_us') <> (to_jsonb(OLD) - 'next_offset_us') THEN
                    RAISE EXCEPTION 'Work origins are append-only';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION docket_work_basis_seal() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE expected bigint; allocated numeric; review boolean;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.sealed THEN RAISE EXCEPTION 'Work quantity basis must be assembled before sealing'; END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'DELETE' OR OLD.sealed OR NOT NEW.sealed
                   OR (to_jsonb(NEW) - 'sealed') <> (to_jsonb(OLD) - 'sealed') THEN
                    RAISE EXCEPTION 'Work quantity basis is append-only';
                END IF;
                SELECT duration_us INTO expected FROM work_session_revisions WHERE ref_id = NEW.revision_ref;
                SELECT coalesce(sum(end_us - start_us), 0), coalesce(bool_or(needs_review), false)
                    INTO allocated, review FROM work_quantity_allocations WHERE revision_ref = NEW.revision_ref;
                IF allocated <> coalesce(expected, 0) OR ((expected IS NULL) <> (NEW.status = 'unknown'))
                   OR (review AND NEW.status <> 'review') THEN
                    RAISE EXCEPTION 'Work quantity basis does not match its revision';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION docket_work_allocation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE capacity bigint; owner_ref text; work_owner text;
            BEGIN
                PERFORM 1 FROM work_quantity_bases WHERE revision_ref = NEW.revision_ref AND NOT sealed FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Work quantity basis is sealed'; END IF;
                SELECT next_offset_us, operator_ref INTO capacity, owner_ref
                    FROM work_quantity_origins WHERE ref_id = NEW.origin_ref FOR UPDATE;
                SELECT w.operator_ref INTO work_owner FROM work_session_revisions r
                    JOIN work_sessions w ON w.ref_id = r.work_ref WHERE r.ref_id = NEW.revision_ref;
                IF owner_ref IS DISTINCT FROM work_owner OR NEW.end_us > capacity THEN
                    RAISE EXCEPTION 'Work allocation is outside its owned origin';
                END IF;
                IF EXISTS (SELECT 1 FROM work_quantity_allocations a WHERE a.revision_ref = NEW.revision_ref
                    AND a.origin_ref = NEW.origin_ref AND a.start_us < NEW.end_us AND a.end_us > NEW.start_us) THEN
                    RAISE EXCEPTION 'Work quantity coordinates overlap';
                END IF;
                RETURN NEW;
            END $$""")
        for table in ("work_quantity_allocations", "work_lineage_edges"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
            )
        op.execute(
            "CREATE TRIGGER trg_work_origin_growth BEFORE UPDATE OR DELETE ON work_quantity_origins FOR EACH ROW EXECUTE FUNCTION docket_work_origin_growth()"
        )
        op.execute(
            "CREATE TRIGGER trg_work_basis_seal BEFORE INSERT OR UPDATE OR DELETE ON work_quantity_bases FOR EACH ROW EXECUTE FUNCTION docket_work_basis_seal()"
        )
        op.execute(
            "CREATE TRIGGER trg_work_allocation_insert BEFORE INSERT ON work_quantity_allocations FOR EACH ROW EXECUTE FUNCTION docket_work_allocation_insert()"
        )


def downgrade() -> None:
    tables = (
        "work_lineage_edges",
        "work_quantity_allocations",
        "work_quantity_bases",
        "work_quantity_origins",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade cannot discard retained work quantity lineage")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_work_allocation_insert()")
        op.execute("DROP FUNCTION docket_work_basis_seal()")
        op.execute("DROP FUNCTION docket_work_origin_growth()")

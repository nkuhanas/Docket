"""retain mixed work-time precision without inventing placement

Revision ID: 20261105d4e5
Revises: 20261105c3d4
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105d4e5"
down_revision = "20261105c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("work_session_revisions") as batch:
        batch.drop_constraint("ck_work_precision", type_="check")
        batch.create_check_constraint(
            "ck_work_precision",
            "time_precision IN ('interval', 'duration', 'incomplete', 'timer', 'composite')",
        )
    op.create_table(
        "work_time_components",
        sa.Column(
            "revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column(
            "source_revision_ref",
            sa.String(40),
            sa.ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0 AND ordinal < 25", name="ck_work_component_order"),
        sa.CheckConstraint(
            "revision_ref <> source_revision_ref", name="ck_work_component_not_self"
        ),
        sa.UniqueConstraint("revision_ref", "source_revision_ref", name="uq_work_component_source"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION docket_work_component_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE own_operator text; source_operator text; own_precision text; source_state text;
            BEGIN
                IF EXISTS (SELECT 1 FROM work_quantity_bases WHERE revision_ref = NEW.revision_ref AND sealed) THEN
                    RAISE EXCEPTION 'Work time composition is sealed';
                END IF;
                SELECT w.operator_ref, r.time_precision INTO own_operator, own_precision
                    FROM work_session_revisions r JOIN work_sessions w ON w.ref_id = r.work_ref WHERE r.ref_id = NEW.revision_ref;
                SELECT w.operator_ref, r.lifecycle INTO source_operator, source_state
                    FROM work_session_revisions r JOIN work_sessions w ON w.ref_id = r.work_ref
                    JOIN work_quantity_bases b ON b.revision_ref = r.ref_id AND b.sealed WHERE r.ref_id = NEW.source_revision_ref;
                IF own_operator IS DISTINCT FROM source_operator OR own_precision <> 'composite' OR source_state <> 'recorded' THEN
                    RAISE EXCEPTION 'Work time source must be sealed, owned and recorded';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute(
            "CREATE TRIGGER trg_work_component_insert BEFORE INSERT ON work_time_components FOR EACH ROW EXECUTE FUNCTION docket_work_component_insert()"
        )
        op.execute(
            "CREATE TRIGGER trg_work_component_immutable BEFORE UPDATE OR DELETE ON work_time_components FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
        op.execute("""CREATE OR REPLACE FUNCTION docket_work_basis_seal() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE expected numeric; stored bigint; time_kind text; allocated numeric; review boolean; has_unknown boolean; leaf_count bigint;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.sealed THEN RAISE EXCEPTION 'Work quantity basis must be assembled before sealing'; END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'DELETE' OR OLD.sealed OR NOT NEW.sealed
                   OR (to_jsonb(NEW) - 'sealed') <> (to_jsonb(OLD) - 'sealed') THEN
                    RAISE EXCEPTION 'Work quantity basis is append-only';
                END IF;
                SELECT duration_us, time_precision INTO stored, time_kind FROM work_session_revisions WHERE ref_id = NEW.revision_ref;
                expected := coalesce(stored, 0); has_unknown := stored IS NULL;
                IF time_kind = 'composite' THEN
                    WITH RECURSIVE sources(ref) AS (
                        SELECT source_revision_ref FROM work_time_components WHERE revision_ref = NEW.revision_ref
                        UNION ALL SELECT c.source_revision_ref FROM work_time_components c JOIN sources s ON c.revision_ref = s.ref
                    ) SELECT coalesce(sum(r.duration_us), 0), coalesce(bool_or(r.duration_us IS NULL), false), count(*)
                        INTO expected, has_unknown, leaf_count FROM sources s JOIN work_session_revisions r ON r.ref_id = s.ref
                        WHERE r.time_precision <> 'composite';
                    IF leaf_count = 0 OR stored IS DISTINCT FROM (CASE WHEN has_unknown THEN NULL ELSE expected END) THEN
                        RAISE EXCEPTION 'Composite work quantity does not match its sources';
                    END IF;
                END IF;
                SELECT coalesce(sum(end_us - start_us), 0), coalesce(bool_or(needs_review), false)
                    INTO allocated, review FROM work_quantity_allocations WHERE revision_ref = NEW.revision_ref;
                IF allocated <> expected OR (has_unknown <> (NEW.status = 'unknown'))
                   OR (review AND NOT has_unknown AND NEW.status <> 'review') THEN
                    RAISE EXCEPTION 'Work quantity basis does not match its revision';
                END IF;
                RETURN NEW;
            END $$""")


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM work_time_components)")).scalar()
        or bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM work_session_revisions WHERE time_precision = 'composite')"
            ),
        ).scalar()
    ):
        raise RuntimeError("Downgrade cannot discard retained composite work")
    op.drop_table("work_time_components")
    with op.batch_alter_table("work_session_revisions") as batch:
        batch.drop_constraint("ck_work_precision", type_="check")
        batch.create_check_constraint(
            "ck_work_precision",
            "time_precision IN ('interval', 'duration', 'incomplete', 'timer')",
        )
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_work_component_insert()")
        op.execute("""CREATE OR REPLACE FUNCTION docket_work_basis_seal() RETURNS trigger LANGUAGE plpgsql AS $$
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

"""retain explicit derived Task/Item title ownership without historical inference"""

import sqlalchemy as sa
from alembic import op

revision = "20261106b2c3"
down_revision = "20261106a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_item_title_ownership",
        sa.Column("item_ref", sa.String(40), sa.ForeignKey("items.ref_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("task_ref", sa.String(40), sa.ForeignKey("tasks.ref_id", ondelete="RESTRICT"), unique=True, nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(40), sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relinquished_by_changeset_ref", sa.String(40), sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT")),
        sa.Column("relinquished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("(relinquished_by_changeset_ref IS NULL) = (relinquished_at IS NULL)", name="ck_task_item_title_ownership_relinquishment"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION retain_task_item_title_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'derived title provenance is retained'; END IF;
              IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (SELECT 1 FROM items i JOIN tasks t ON t.item_ref = i.ref_id
                  WHERE i.ref_id = NEW.item_ref AND t.ref_id = NEW.task_ref
                    AND i.created_by_changeset_ref = NEW.created_by_changeset_ref
                    AND t.created_by_changeset_ref = NEW.created_by_changeset_ref
                    AND i.title = t.title) THEN
                  RAISE EXCEPTION 'title ownership requires a newly derived pair'; END IF;
                RETURN NEW;
              END IF;
              IF (NEW.item_ref,NEW.task_ref,NEW.created_by_changeset_ref,NEW.created_at)
                  IS DISTINCT FROM (OLD.item_ref,OLD.task_ref,OLD.created_by_changeset_ref,OLD.created_at)
                OR OLD.relinquished_at IS NOT NULL OR NEW.relinquished_at IS NULL THEN
                RAISE EXCEPTION 'title ownership can only be relinquished once'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER retain_task_item_title_ownership BEFORE INSERT OR UPDATE OR DELETE
              ON task_item_title_ownership FOR EACH ROW EXECUTE FUNCTION retain_task_item_title_ownership();
        """)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM task_item_title_ownership")):
        raise RuntimeError("cannot discard retained derived title provenance; use forward repair")
    op.drop_table("task_item_title_ownership")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION retain_task_item_title_ownership()")

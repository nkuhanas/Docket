"""extend the shared provider queue with Tasks operations and honest supersession"""

import sqlalchemy as sa
from alembic import op

revision = "20261106e5f6"
down_revision = "20261106d4e5"
branch_labels = None
depends_on = None

_CALENDAR = (
    "calendar_create_event", "calendar_update_event", "calendar_update_reminders",
    "calendar_cancel_event", "calendar_configure_lane", "calendar_delete_lane",
)
_TASKS = ("tasks_insert", "tasks_patch", "tasks_move", "tasks_delete")
_STATES = ("pending", "running", "succeeded", "failed", "reconciliation_required")


def _choices(field: str, values: tuple[str, ...]) -> str:
    return field + " IN (" + ",".join(repr(value) for value in values) + ")"


def _constraints(tasks: bool) -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("ck_operations_type", type_="check")
        batch.drop_constraint("ck_operations_status", type_="check")
        batch.create_check_constraint(
            "ck_operations_type", _choices("operation_type", _CALENDAR + (_TASKS if tasks else ())),
        )
        batch.create_check_constraint(
            "ck_operations_status",
            _choices("status", _STATES + ("partial_failed",) + (("superseded",) if tasks else ())),
        )
    with op.batch_alter_table("operation_targets") as batch:
        batch.drop_constraint("ck_operation_targets_status", type_="check")
        batch.create_check_constraint(
            "ck_operation_targets_status",
            _choices("status", _STATES + (("superseded",) if tasks else ())),
        )


def upgrade() -> None:
    _constraints(True)
    op.add_column("operation_targets", sa.Column("recovery", sa.JSON()))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text(
        "SELECT count(*) FROM operations WHERE operation_type LIKE 'tasks_%' OR status = 'superseded'"
    )) or bind.scalar(sa.text(
        "SELECT count(*) FROM operation_targets WHERE recovery IS NOT NULL OR status = 'superseded'"
    )):
        raise RuntimeError("cannot discard retained Tasks delivery history; use forward repair")
    with op.batch_alter_table("operation_targets") as batch:
        batch.drop_column("recovery")
    _constraints(False)

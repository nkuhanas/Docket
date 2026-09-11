"""retain uncommittable draft inputs independently of compiled snapshots

Revision ID: 20260911c2f1
Revises: 20260911b1e0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911c2f1"
down_revision: str | None = "20260911b1e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No historical request is reinterpreted, backfilled or executed. NULL
    # identifies rows that have no independent staged-input snapshot yet.
    for table in ("change_sets", "change_set_revisions"):
        op.add_column(table, sa.Column("staged_actions_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Rehearsal only after new drafts exist: restore the pre-migration backup
    # or use a forward repair in production to avoid losing failed draft input.
    for table in ("change_set_revisions", "change_sets"):
        op.drop_column(table, "staged_actions_json")

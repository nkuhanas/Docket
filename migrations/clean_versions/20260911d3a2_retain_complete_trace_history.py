"""retain complete trace history independently of projection page limits

Revision ID: 20260911d3a2
Revises: 20260911c2f1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911d3a2"
down_revision: str | None = "20260911c2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversational_tool_traces"
_CONSTRAINT = "ck_conversational_tool_traces_last_ordinal"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, "last_ordinal >= 0")


def downgrade() -> None:
    # Never discard trace evidence to make an old implementation fit. An image
    # rollback cannot restore its 100-call assumption once longer traces exist.
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM conversational_tool_traces WHERE last_ordinal > 100)"
    )):
        raise RuntimeError("Trace history exceeds 100 calls; downgrade would discard evidence")
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, "last_ordinal BETWEEN 0 AND 100")

"""preserve full Google Task titles through required Items

Revision ID: 20261106a1b2
Revises: 20261105a3b4
"""

import sqlalchemy as sa
from alembic import op

revision = "20261106a1b2"
down_revision = "20261105a3b4"
branch_labels = None
depends_on = None


def _resize(size: int, old_size: int) -> None:
    for table in ("items", "tasks"):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.alter_column(
                    "title", existing_type=sa.String(old_size), type_=sa.String(size),
                    existing_nullable=False,
                )
        else:
            op.alter_column(
                table, "title", existing_type=sa.String(old_size), type_=sa.String(size),
                existing_nullable=False,
            )


def upgrade() -> None:
    _resize(1024, 512)


def downgrade() -> None:
    for table in ("items", "tasks"):
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table} WHERE length(title) > 512")):
            raise RuntimeError("cannot truncate retained Task/Item titles; use forward repair")
    _resize(512, 1024)

"""Give provider identities typed public source references.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _source_ref() -> str:
    value = (int(time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "src_" + "".join(encoded)


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("ref_id", sa.String(length=40)))

    bind = op.get_bind()
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
    )
    for account_id in bind.scalars(sa.select(accounts.c.id)):
        bind.execute(
            accounts.update().where(accounts.c.id == account_id).values(ref_id=_source_ref())
        )

    with op.batch_alter_table("accounts") as batch:
        batch.alter_column("ref_id", existing_type=sa.String(length=40), nullable=False)
        batch.create_unique_constraint("uq_accounts_ref_id", ["ref_id"])


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_accounts_ref_id", type_="unique")
        batch.drop_column("ref_id")

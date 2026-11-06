"""fit the already-admitted temporal_projection discriminator in Calendar bindings"""

import sqlalchemy as sa
from alembic import op

revision = "20261106f6a7"
down_revision = "20261106e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_event_bindings") as batch:
        batch.alter_column("target_kind", existing_type=sa.String(16), type_=sa.String(32),
                           existing_nullable=False)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM provider_event_bindings WHERE length(target_kind) > 16"
    )):
        raise RuntimeError("cannot truncate retained Calendar projection bindings; use forward repair")
    with op.batch_alter_table("provider_event_bindings") as batch:
        batch.alter_column("target_kind", existing_type=sa.String(32), type_=sa.String(16),
                           existing_nullable=False)

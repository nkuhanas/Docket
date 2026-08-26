"""Retire residual unresolved alpha Gmail housekeeping cards.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)
    connection.execute(
        sa.text(
            "UPDATE approvals SET status = 'superseded', responded_at = :now "
            "WHERE status = 'pending' AND action_revision_id IN ("
            "SELECT ar.id FROM action_revisions ar "
            "WHERE ar.action_type IN ('gmail_archive_message', 'gmail_mark_read'))"
        ),
        {"now": now},
    )
    connection.execute(
        sa.text(
            "UPDATE actions SET status = 'superseded', updated_at = :now "
            "WHERE action_type IN ('gmail_archive_message', 'gmail_mark_read') "
            "AND status IN ('available', 'approval_pending', 'failed')"
        ),
        {"now": now},
    )
    connection.execute(
        sa.text(
            "UPDATE queue_items SET status = 'completed', presentation = 'awareness', "
            "resolved_at = :now, resolution_code = 'alpha_housekeeping_retired', "
            "resolution_note = 'Autonomous Gmail housekeeping is retired; no decision required.', "
            "version = version + 1, updated_at = :now "
            "WHERE status IN ('pending', 'awaiting_approval', 'snoozed', 'failed') "
            "AND EXISTS (SELECT 1 FROM actions a WHERE a.queue_item_id = queue_items.id "
            "AND a.action_type IN ('gmail_archive_message', 'gmail_mark_read')) "
            "AND NOT EXISTS (SELECT 1 FROM operations o JOIN action_revisions ar "
            "ON ar.id = o.action_revision_id JOIN actions a ON a.id = ar.action_id "
            "WHERE a.queue_item_id = queue_items.id "
            "AND o.status IN ('pending', 'running', 'reconciliation_required'))"
        ),
        {"now": now},
    )


def downgrade() -> None:
    # Alpha housekeeping intent is deliberately not reconstructed. Restoring an
    # obsolete approval would manufacture authority that no longer exists.
    pass

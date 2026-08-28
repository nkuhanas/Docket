"""Associate sender-label handles with exact email IdentityHandles.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sender_identity_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sender_identity_handle_id", sa.Uuid(), nullable=False),
        sa.Column("email_identity_handle_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("basis_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_sender_identity_emails_status",
        ),
        sa.ForeignKeyConstraint(
            ["sender_identity_handle_id"],
            ["identity_handles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["email_identity_handle_id"],
            ["identity_handles.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sender_identity_handle_id",
            "email_identity_handle_id",
            name="uq_sender_identity_email_pair",
        ),
    )
    op.create_index(
        "uq_sender_identity_emails_active_email",
        "sender_identity_emails",
        ["email_identity_handle_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_sender_identity_emails_sender_status",
        "sender_identity_emails",
        ["sender_identity_handle_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sender_identity_emails_sender_status",
        table_name="sender_identity_emails",
    )
    op.drop_index(
        "uq_sender_identity_emails_active_email",
        table_name="sender_identity_emails",
    )
    op.drop_table("sender_identity_emails")

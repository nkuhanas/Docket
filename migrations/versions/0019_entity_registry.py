"""Add the emergent entity registry and resolution ledger.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLASSES = "'institution', 'organization', 'course', 'person', 'location', 'project', 'service'"


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_class", sa.String(length=32), nullable=False),
        sa.Column("canonical_name", sa.String(length=512), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("merged_into_id", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"entity_class IN ({_CLASSES})", name="ck_entities_class"),
        sa.CheckConstraint(
            "status IN ('active', 'provisional', 'merged', 'archived')",
            name="ck_entities_status",
        ),
        sa.ForeignKeyConstraint(["merged_into_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_entities_active_identity",
        "entities",
        ["entity_class", "normalized_name"],
        unique=True,
        sqlite_where=sa.text("status IN ('active', 'provisional')"),
        postgresql_where=sa.text("status IN ('active', 'provisional')"),
    )
    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=512), nullable=False),
        sa.Column("normalized_alias", sa.String(length=512), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "normalized_alias", name="uq_entity_aliases_identity"),
    )
    op.create_table(
        "entity_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("predicate", sa.String(length=128), nullable=False),
        sa.Column("object_entity_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('active', 'retracted')", name="ck_entity_relations_status"),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["object_entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_entity_id",
            "predicate",
            "object_entity_id",
            name="uq_entity_relations_triple",
        ),
    )
    op.create_table(
        "entity_resolutions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_class", sa.String(length=32), nullable=False),
        sa.Column("mention", sa.String(length=512), nullable=False),
        sa.Column("normalized_mention", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("resolved_entity_id", sa.Uuid()),
        sa.Column("candidate_entity_ids", sa.JSON(), nullable=False),
        sa.Column("source_item_id", sa.Uuid()),
        sa.Column("semantic_candidate_id", sa.Uuid()),
        sa.Column("corrected_resolution_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('resolved', 'unresolved', 'ambiguous', 'provisional')",
            name="ck_entity_resolutions_state",
        ),
        sa.ForeignKeyConstraint(["resolved_entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["semantic_candidate_id"], ["semantic_candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["corrected_resolution_id"], ["entity_resolutions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("entity_resolutions")
    op.drop_table("entity_relations")
    op.drop_table("entity_aliases")
    op.drop_index("uq_entities_active_identity", table_name="entities")
    op.drop_table("entities")

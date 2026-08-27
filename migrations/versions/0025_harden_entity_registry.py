"""Constrain and index canonical entity relationships.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATES = (
    "'works_for', 'member_of', 'affiliated_with', 'advises', 'instructs', "
    "'reports_to', 'collaborates_with', 'knows', 'friend_of', 'classmate_of', "
    "'leads', 'participates_in', 'located_at', 'uses', 'supports'"
)


def upgrade() -> None:
    op.create_index(
        "ix_entity_aliases_normalized_alias",
        "entity_aliases",
        ["normalized_alias"],
    )
    with op.batch_alter_table("entity_relations") as batch:
        batch.create_check_constraint(
            "ck_entity_relations_predicate",
            f"predicate IN ({_PREDICATES})",
        )
        batch.create_index(
            "ix_entity_relations_subject_predicate",
            ["subject_entity_id", "predicate"],
        )
        batch.create_index(
            "ix_entity_relations_object_predicate",
            ["object_entity_id", "predicate"],
        )


def downgrade() -> None:
    with op.batch_alter_table("entity_relations") as batch:
        batch.drop_index("ix_entity_relations_object_predicate")
        batch.drop_index("ix_entity_relations_subject_predicate")
        batch.drop_constraint("ck_entity_relations_predicate", type_="check")
    op.drop_index("ix_entity_aliases_normalized_alias", table_name="entity_aliases")

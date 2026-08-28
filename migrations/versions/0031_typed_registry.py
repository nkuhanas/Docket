"""Add provenance-bearing typed personal registry objects.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.TextClause:
    return sa.text("'[]'")


def _legacy_source_ref(entity_ref: str) -> str:
    return f"src_{entity_ref.split('_', 1)[1]}"


def _canonical_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_registry_tables() -> None:
    op.create_table(
        "provenance_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("external_ref", sa.String(length=512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('legacy_canonical_object', 'imported', 'external')",
            name="ck_provenance_sources_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "source_kind", "external_ref", name="uq_provenance_source_origin"
        ),
    )
    op.create_table(
        "person_profiles",
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("preferred_name", sa.String(length=512)),
        sa.Column("pronouns", sa.String(length=128)),
        sa.Column("is_operator", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_index(
        "uq_person_profiles_operator",
        "person_profiles",
        ["is_operator"],
        unique=True,
        postgresql_where=sa.text("is_operator"),
        sqlite_where=sa.text("is_operator = 1"),
    )
    op.create_table(
        "organization_profiles",
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("parent_entity_id", sa.Uuid()),
        sa.Column("organization_type", sa.String(length=128)),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_kind IN ('organization', 'institution')",
            name="ck_organization_profiles_kind",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_table(
        "identity_handles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("handle_type", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=1024), nullable=False),
        sa.Column("normalized_value", sa.String(length=1024), nullable=False),
        sa.Column("entity_id", sa.Uuid()),
        sa.Column("binding_rule", sa.String(length=64)),
        sa.Column("binding_basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('unbound', 'bound', 'historical', 'retracted')",
            name="ck_identity_handles_status",
        ),
        sa.CheckConstraint(
            "((status = 'bound' AND entity_id IS NOT NULL AND binding_rule IS NOT NULL) "
            "OR (status <> 'bound'))",
            name="ck_identity_handles_bound_target",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
        sa.UniqueConstraint(
            "handle_type", "normalized_value", name="uq_identity_handles_value"
        ),
    )
    op.create_index(
        "ix_identity_handles_entity_status",
        "identity_handles",
        ["entity_id", "status"],
    )
    op.create_table(
        "identity_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("identity_handle_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("binding_rule", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_identity_bindings_status",
        ),
        sa.ForeignKeyConstraint(
            ["identity_handle_id"], ["identity_handles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_bindings_handle_status",
        "identity_bindings",
        ["identity_handle_id", "status"],
    )

    op.create_table(
        "affiliations",
        *_canonical_columns(),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("organization_entity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=512)),
        sa.Column("domain", sa.String(length=512)),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_affiliations_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_affiliations_interval",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_affiliations_subject_status",
        "affiliations",
        ["subject_entity_id", "status"],
    )
    op.create_index(
        "ix_affiliations_organization_status",
        "affiliations",
        ["organization_entity_id", "status"],
    )
    op.create_table(
        "relationships",
        *_canonical_columns(),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("object_entity_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=128)),
        sa.Column("context", sa.Text()),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_relationships_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_relationships_interval",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["object_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_relationships_subject_status",
        "relationships",
        ["subject_entity_id", "status"],
    )
    op.create_index(
        "ix_relationships_object_status",
        "relationships",
        ["object_entity_id", "status"],
    )
    op.create_table(
        "facts",
        *_canonical_columns(),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("predicate", sa.String(length=255), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')", name="ck_facts_status"
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_facts_interval",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index(
        "ix_facts_subject_predicate_status",
        "facts",
        ["subject_entity_id", "predicate", "status"],
    )
    op.create_table(
        "interactions",
        *_canonical_columns(),
        sa.Column("interaction_type", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("event_ref", sa.String(length=40)),
        sa.Column("place_entity_id", sa.Uuid()),
        sa.Column("organization_refs", sa.JSON(), server_default=_json_list(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_interactions_status",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= occurred_at",
            name="ck_interactions_interval",
        ),
        sa.ForeignKeyConstraint(
            ["place_entity_id"], ["entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id"),
    )
    op.create_index("ix_interactions_occurred", "interactions", ["occurred_at"])
    op.create_table(
        "interaction_participants",
        sa.Column("interaction_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("interaction_id", "entity_id", "role"),
        sa.UniqueConstraint(
            "interaction_id", "entity_id", "role", name="uq_interaction_participant"
        ),
    )


def _evolve_and_backfill_entities() -> None:
    with op.batch_alter_table("entities") as batch:
        batch.drop_constraint("ck_entities_class", type_="check")
        batch.create_check_constraint(
            "ck_entities_class",
            "entity_class IN ('institution', 'organization', 'course', 'person', "
            "'course_section', 'place', 'location', 'project', 'service')",
        )
        batch.add_column(
            sa.Column(
                "registration_state",
                sa.String(length=32),
                server_default="legacy_candidate",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("basis_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("decision_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(
            sa.Column("source_refs", sa.JSON(), server_default=_json_list(), nullable=False)
        )
        batch.add_column(sa.Column("created_by_changeset_ref", sa.String(length=40)))
        batch.add_column(
            sa.Column(
                "provenance_status",
                sa.String(length=32),
                server_default="legacy_preledger",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_entities_registration_state",
            "registration_state IN ('registered', 'legacy_candidate', 'historical')",
        )
        batch.create_check_constraint(
            "ck_entities_provenance_status",
            "provenance_status IN ('complete', 'legacy_preledger')",
        )

    bind = op.get_bind()
    entities = sa.table(
        "entities",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
        sa.column("entity_class", sa.String()),
        sa.column("canonical_name", sa.String()),
        sa.column("authority", sa.String()),
        sa.column("attributes", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("registration_state", sa.String()),
        sa.column("basis_refs", sa.JSON()),
        sa.column("source_refs", sa.JSON()),
    )
    sources = sa.table(
        "provenance_sources",
        sa.column("id", sa.Uuid()),
        sa.column("ref_id", sa.String()),
        sa.column("source_kind", sa.String()),
        sa.column("external_ref", sa.String()),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("content_hash", sa.String()),
        sa.column("metadata_json", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    person_profiles = sa.table(
        "person_profiles",
        sa.column("entity_id", sa.Uuid()),
        sa.column("preferred_name", sa.String()),
        sa.column("pronouns", sa.String()),
        sa.column("is_operator", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    organization_profiles = sa.table(
        "organization_profiles",
        sa.column("entity_id", sa.Uuid()),
        sa.column("entity_kind", sa.String()),
        sa.column("parent_entity_id", sa.Uuid()),
        sa.column("organization_type", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    rows = list(bind.execute(sa.select(entities)))
    source_rows: list[dict[str, Any]] = []
    person_rows: list[dict[str, Any]] = []
    organization_rows: list[dict[str, Any]] = []
    for row in rows:
        metadata = {
            "entity_ref": row.ref_id,
            "entity_class": row.entity_class,
            "canonical_name": row.canonical_name,
            "legacy_authority": row.authority,
        }
        serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        source_ref = _legacy_source_ref(row.ref_id)
        source_rows.append(
            {
                "id": uuid.uuid4(),
                "ref_id": source_ref,
                "source_kind": "legacy_canonical_object",
                "external_ref": row.ref_id,
                "observed_at": row.created_at or now,
                "content_hash": hashlib.sha256(serialized.encode()).hexdigest(),
                "metadata_json": metadata,
                "created_at": now,
            }
        )
        bind.execute(
            entities.update()
            .where(entities.c.id == row.id)
            .values(
                registration_state=(
                    "registered" if row.authority == "explicit_user" else "legacy_candidate"
                ),
                basis_refs=[source_ref],
                source_refs=[source_ref],
            )
        )
        attributes = row.attributes if isinstance(row.attributes, dict) else {}
        if row.entity_class == "person":
            person_rows.append(
                {
                    "entity_id": row.id,
                    "preferred_name": attributes.get("preferred_name"),
                    "pronouns": attributes.get("pronouns"),
                    "is_operator": attributes.get("is_operator") is True,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        elif row.entity_class in {"organization", "institution"}:
            organization_rows.append(
                {
                    "entity_id": row.id,
                    "entity_kind": row.entity_class,
                    "parent_entity_id": None,
                    "organization_type": attributes.get("organization_type"),
                    "description": attributes.get("description"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
    if source_rows:
        op.bulk_insert(sources, source_rows)
    if person_rows:
        op.bulk_insert(person_profiles, person_rows)
    if organization_rows:
        op.bulk_insert(organization_profiles, organization_rows)


def upgrade() -> None:
    _create_registry_tables()
    _evolve_and_backfill_entities()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_provenance_sources_immutable BEFORE UPDATE OR DELETE "
            "ON provenance_sources FOR EACH ROW EXECUTE FUNCTION "
            "docket_reject_immutable_provenance()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_provenance_sources_immutable ON provenance_sources"
        )
    op.drop_table("interaction_participants")
    op.drop_index("ix_interactions_occurred", table_name="interactions")
    op.drop_table("interactions")
    op.drop_index("ix_facts_subject_predicate_status", table_name="facts")
    op.drop_table("facts")
    op.drop_index("ix_relationships_object_status", table_name="relationships")
    op.drop_index("ix_relationships_subject_status", table_name="relationships")
    op.drop_table("relationships")
    op.drop_index("ix_affiliations_organization_status", table_name="affiliations")
    op.drop_index("ix_affiliations_subject_status", table_name="affiliations")
    op.drop_table("affiliations")
    op.drop_index("ix_identity_bindings_handle_status", table_name="identity_bindings")
    op.drop_table("identity_bindings")
    op.drop_index("ix_identity_handles_entity_status", table_name="identity_handles")
    op.drop_table("identity_handles")
    op.drop_table("organization_profiles")
    op.drop_index("uq_person_profiles_operator", table_name="person_profiles")
    op.drop_table("person_profiles")
    with op.batch_alter_table("entities") as batch:
        batch.drop_constraint("ck_entities_provenance_status", type_="check")
        batch.drop_constraint("ck_entities_registration_state", type_="check")
        batch.drop_constraint("ck_entities_class", type_="check")
        batch.drop_column("provenance_status")
        batch.drop_column("created_by_changeset_ref")
        batch.drop_column("source_refs")
        batch.drop_column("decision_refs")
        batch.drop_column("basis_refs")
        batch.drop_column("registration_state")
        batch.create_check_constraint(
            "ck_entities_class",
            "entity_class IN ('institution', 'organization', 'course', 'person', "
            "'location', 'project', 'service')",
        )
    op.drop_table("provenance_sources")

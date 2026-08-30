import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, TimestampMixin, utc_now


class CanonicalProvenanceMixin:
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)


class Source(Base):
    """External/imported evidence that has no richer typed Docket object."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('imported', 'external', 'gmail', 'google_calendar', 'attachment')",
            name="ck_sources_kind",
        ),
        UniqueConstraint("source_kind", "external_ref", name="uq_provenance_source_origin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("src")
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class PersonProfile(TimestampMixin, Base):
    __tablename__ = "person_profiles"
    __table_args__ = (
        Index(
            "uq_person_profiles_operator",
            "is_operator",
            unique=True,
            postgresql_where=text("is_operator"),
            sqlite_where=text("is_operator = 1"),
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    preferred_name: Mapped[str | None] = mapped_column(String(512))
    pronouns: Mapped[str | None] = mapped_column(String(128))
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OrganizationInstitutionProfile(TimestampMixin, Base):
    __tablename__ = "organization_institution_profiles"
    __table_args__ = (
        CheckConstraint(
            "entity_kind IN ('organization', 'institution')",
            name="ck_organization_institution_profiles_kind",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    organization_type: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)


class IdentityHandle(TimestampMixin, Base):
    __tablename__ = "identity_handles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unbound', 'bound', 'historical', 'retracted')",
            name="ck_identity_handles_status",
        ),
        CheckConstraint(
            "((status = 'bound' AND entity_id IS NOT NULL AND binding_rule IS NOT NULL) "
            "OR (status <> 'bound'))",
            name="ck_identity_handles_bound_target",
        ),
        UniqueConstraint("handle_type", "normalized_value", name="uq_identity_handles_value"),
        Index("ix_identity_handles_entity_status", "entity_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("idn")
    )
    handle_type: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(1024), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(1024), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    binding_rule: Mapped[str | None] = mapped_column(String(64))
    binding_basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="unbound", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str | None] = mapped_column(String(40))


class SenderIdentityEmail(TimestampMixin, Base):
    """Time-scoped exact email membership for one sender-label index handle."""

    __tablename__ = "sender_identity_emails"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_sender_identity_emails_status",
        ),
        UniqueConstraint(
            "sender_identity_handle_id",
            "email_identity_handle_id",
            name="uq_sender_identity_email_pair",
        ),
        Index(
            "uq_sender_identity_emails_active_email",
            "email_identity_handle_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_sender_identity_emails_sender_status",
            "sender_identity_handle_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sender_identity_handle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_handles.id", ondelete="RESTRICT"), nullable=False
    )
    email_identity_handle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_handles.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)


class IdentityBinding(CanonicalProvenanceMixin, Base):
    """Historical binding state for one public IdentityHandle."""

    __tablename__ = "identity_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_identity_bindings_status",
        ),
        Index("ix_identity_bindings_handle_status", "identity_handle_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_handle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_handles.id", ondelete="RESTRICT"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    binding_rule: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class Affiliation(CanonicalProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "affiliations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_affiliations_status",
        ),
        Index("ix_affiliations_subject_status", "subject_entity_id", "status"),
        Index("ix_affiliations_organization_status", "organization_entity_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("aff")
    )
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    organization_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str | None] = mapped_column(String(512))
    domain: Mapped[str | None] = mapped_column(String(512))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)


class Relationship(CanonicalProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "relationships"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_relationships_status",
        ),
        Index("ix_relationships_subject_status", "subject_entity_id", "status"),
        Index("ix_relationships_object_status", "object_entity_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("rel")
    )
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    object_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    relationship_type: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)


class Fact(CanonicalProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_facts_status",
        ),
        CheckConstraint(
            "subject_ref LIKE 'ent\\_%' ESCAPE '\\' OR "
            "subject_ref LIKE 'item\\_%' ESCAPE '\\'",
            name="ck_facts_subject_ref",
        ),
        Index("ix_facts_subject_predicate_status", "subject_ref", "predicate", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("fact")
    )
    subject_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)


class Interaction(CanonicalProvenanceMixin, TimestampMixin, Base):
    __tablename__ = "interactions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_interactions_status",
        ),
        Index("ix_interactions_occurred", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("int")
    )
    interaction_type: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    event_ref: Mapped[str | None] = mapped_column(String(40))
    place_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    organization_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)


class InteractionParticipant(Base):
    __tablename__ = "interaction_participants"
    __table_args__ = (
        UniqueConstraint("interaction_id", "entity_id", "role", name="uq_interaction_participant"),
    )

    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(128), primary_key=True, default="participant")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


def _reject_immutable(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is immutable")


event.listen(Source, "before_update", _reject_immutable)
event.listen(Source, "before_delete", _reject_immutable)

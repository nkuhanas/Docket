from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.domain.enums import IntentAuthority
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    Entity,
    EntityAlias,
    EntityRelation,
    EntityResolution,
)
from docket.schemas.entities import EntityResolutionResult, EntityResult
from docket.schemas.triage import EntityClass

_SPACE = re.compile(r"\s+")


def normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return _SPACE.sub(" ", normalized)


def serialize_entity(entity: Entity) -> EntityResult:
    return EntityResult(
        entity_id=entity.id,
        entity_class=entity.entity_class,
        canonical_name=entity.canonical_name,
        status=entity.status,
        attributes=dict(entity.attributes),
        authority=entity.authority,
        version=entity.version,
        merged_into_id=entity.merged_into_id,
    )


class EntityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _matches(self, entity_class: EntityClass, normalized: str) -> list[Entity]:
        alias_entity_ids = select(EntityAlias.entity_id).where(
            EntityAlias.normalized_alias == normalized
        )
        return list(
            self.session.scalars(
                select(Entity)
                .where(
                    Entity.entity_class == entity_class,
                    Entity.status.in_(("active", "provisional")),
                    or_(
                        Entity.normalized_name == normalized,
                        Entity.id.in_(alias_entity_ids),
                    ),
                )
                .order_by(Entity.status, Entity.canonical_name, Entity.id)
            )
        )

    def resolve(
        self,
        *,
        entity_class: EntityClass,
        mention: str,
        source_item_id: uuid.UUID | None = None,
        semantic_candidate_id: uuid.UUID | None = None,
        allow_provisional: bool = False,
    ) -> EntityResolutionResult:
        normalized = normalize_entity_name(mention)
        if not normalized:
            raise DocketError(
                code="invalid_entity_mention",
                message="Entity mention cannot be empty after normalization.",
            )
        matches = self._matches(entity_class, normalized)
        if not matches and allow_provisional:
            provisional = self.create(
                entity_class=entity_class,
                canonical_name=mention,
                attributes={},
                authority=IntentAuthority.INFERRED,
                provisional=True,
            )
            entity = self.session.get(Entity, provisional.entity_id)
            assert entity is not None
            matches = [entity]
            state = "provisional"
        elif len(matches) == 1:
            state = "provisional" if matches[0].status == "provisional" else "resolved"
        elif matches:
            state = "ambiguous"
        else:
            state = "unresolved"
        resolved = matches[0] if len(matches) == 1 else None
        resolution = EntityResolution(
            entity_class=entity_class,
            mention=mention,
            normalized_mention=normalized,
            state=state,
            resolved_entity_id=resolved.id if resolved is not None else None,
            candidate_entity_ids=[str(entity.id) for entity in matches],
            source_item_id=source_item_id,
            semantic_candidate_id=semantic_candidate_id,
        )
        self.session.add(resolution)
        self.session.flush()
        return EntityResolutionResult(
            resolution_id=resolution.id,
            entity_class=entity_class,
            mention=mention,
            state=state,
            resolved_entity=serialize_entity(resolved) if resolved is not None else None,
            candidates=[serialize_entity(entity) for entity in matches],
        )

    def create(
        self,
        *,
        entity_class: EntityClass,
        canonical_name: str,
        attributes: dict[str, Any],
        authority: IntentAuthority,
        provisional: bool = False,
        actor_type: str = "docket",
        actor_id: str | None = None,
    ) -> EntityResult:
        normalized = normalize_entity_name(canonical_name)
        existing = self.session.scalar(
            select(Entity).where(
                Entity.entity_class == entity_class,
                Entity.normalized_name == normalized,
                Entity.status.in_(("active", "provisional")),
            )
        )
        if existing is not None:
            if (
                existing.status == "provisional"
                and not provisional
                and authority != IntentAuthority.INFERRED
            ):
                existing.status = "active"
                existing.authority = authority.value
                existing.attributes = {**existing.attributes, **attributes}
                existing.version += 1
            return serialize_entity(existing)
        entity = Entity(
            entity_class=entity_class,
            canonical_name=canonical_name.strip(),
            normalized_name=normalized,
            status="provisional" if provisional else "active",
            attributes=attributes,
            authority=authority.value,
        )
        self.session.add(entity)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="entity.created",
                entity_type="entity",
                entity_id=entity.id,
                actor_type=actor_type,
                actor_id=actor_id,
                data={
                    "entity_class": entity_class,
                    "authority": authority.value,
                    "provisional": provisional,
                },
            )
        )
        return serialize_entity(entity)

    def update(
        self,
        *,
        entity_id: uuid.UUID,
        expected_version: int,
        canonical_name: str | None,
        attributes: dict[str, Any] | None,
        authority: IntentAuthority,
        actor_id: str | None = None,
    ) -> EntityResult:
        entity = self.session.scalar(select(Entity).where(Entity.id == entity_id).with_for_update())
        if entity is None or entity.status not in {"active", "provisional"}:
            raise DocketError(code="entity_not_found", message="Active entity was not found.")
        if entity.version != expected_version:
            raise DocketError(
                code="entity_version_changed",
                message="The entity changed after it was read.",
                details={"current_version": entity.version},
            )
        if canonical_name is not None:
            entity.canonical_name = canonical_name.strip()
            entity.normalized_name = normalize_entity_name(canonical_name)
        if attributes is not None:
            entity.attributes = dict(attributes)
        entity.authority = authority.value
        if authority != IntentAuthority.INFERRED:
            entity.status = "active"
        entity.version += 1
        self.session.add(
            AuditEvent(
                event_type="entity.updated",
                entity_type="entity",
                entity_id=entity.id,
                actor_type="hermes" if actor_id else "docket",
                actor_id=actor_id,
                data={"authority": authority.value, "version": entity.version},
            )
        )
        return serialize_entity(entity)

    def add_alias(
        self,
        *,
        entity_id: uuid.UUID,
        alias: str,
        authority: IntentAuthority,
        confidence: float = 1.0,
    ) -> EntityResult:
        entity = self.session.get(Entity, entity_id)
        if entity is None or entity.status not in {"active", "provisional"}:
            raise DocketError(code="entity_not_found", message="Active entity was not found.")
        normalized = normalize_entity_name(alias)
        existing = self.session.scalar(
            select(EntityAlias).where(
                EntityAlias.entity_id == entity.id,
                EntityAlias.normalized_alias == normalized,
            )
        )
        if existing is None:
            self.session.add(
                EntityAlias(
                    entity_id=entity.id,
                    alias=alias.strip(),
                    normalized_alias=normalized,
                    authority=authority.value,
                    confidence=confidence,
                )
            )
            entity.version += 1
        return serialize_entity(entity)

    def relate(
        self,
        *,
        subject_entity_id: uuid.UUID,
        predicate: str,
        object_entity_id: uuid.UUID,
        authority: IntentAuthority,
        attributes: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        if subject_entity_id == object_entity_id:
            raise DocketError(
                code="invalid_entity_relation", message="An entity cannot relate to itself."
            )
        for entity_id in (subject_entity_id, object_entity_id):
            entity = self.session.get(Entity, entity_id)
            if entity is None or entity.status not in {"active", "provisional"}:
                raise DocketError(
                    code="entity_not_found", message="A related entity was not found."
                )
        relation = self.session.scalar(
            select(EntityRelation).where(
                EntityRelation.subject_entity_id == subject_entity_id,
                EntityRelation.predicate == predicate,
                EntityRelation.object_entity_id == object_entity_id,
            )
        )
        if relation is None:
            relation = EntityRelation(
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                object_entity_id=object_entity_id,
                authority=authority.value,
                attributes=attributes or {},
            )
            self.session.add(relation)
            self.session.flush()
        elif relation.status == "retracted":
            relation.status = "active"
            relation.authority = authority.value
            relation.attributes = attributes or {}
            relation.version += 1
        return relation.id

    def merge(
        self,
        *,
        survivor_id: uuid.UUID,
        absorbed_id: uuid.UUID,
        authority: IntentAuthority,
        actor_id: str | None = None,
    ) -> EntityResult:
        survivor = self.session.get(Entity, survivor_id)
        absorbed = self.session.get(Entity, absorbed_id)
        if survivor is None or absorbed is None:
            raise DocketError(code="entity_not_found", message="Merge entity was not found.")
        if absorbed.status == "merged" and absorbed.merged_into_id == survivor.id:
            return serialize_entity(survivor)
        if survivor.entity_class != absorbed.entity_class or survivor.id == absorbed.id:
            raise DocketError(
                code="invalid_entity_merge",
                message="Only distinct entities of the same class can be merged.",
            )
        aliases = list(
            self.session.scalars(select(EntityAlias).where(EntityAlias.entity_id == absorbed.id))
        )
        for alias in aliases:
            duplicate = self.session.scalar(
                select(EntityAlias.id).where(
                    EntityAlias.entity_id == survivor.id,
                    EntityAlias.normalized_alias == alias.normalized_alias,
                )
            )
            if duplicate is None:
                alias.entity_id = survivor.id
            else:
                self.session.delete(alias)
        if normalize_entity_name(absorbed.canonical_name) != survivor.normalized_name:
            self.add_alias(
                entity_id=survivor.id,
                alias=absorbed.canonical_name,
                authority=authority,
            )
        absorbed.status = "merged"
        absorbed.merged_into_id = survivor.id
        absorbed.version += 1
        survivor.version += 1
        self.session.add(
            AuditEvent(
                event_type="entity.merged",
                entity_type="entity",
                entity_id=survivor.id,
                actor_type="hermes" if actor_id else "docket",
                actor_id=actor_id,
                data={"absorbed_entity_id": str(absorbed.id), "authority": authority.value},
            )
        )
        return serialize_entity(survivor)

    def rebind_resolution(
        self,
        *,
        resolution_id: uuid.UUID,
        entity_id: uuid.UUID,
        actor_id: str,
    ) -> EntityResolutionResult:
        previous = self.session.get(EntityResolution, resolution_id)
        entity = self.session.get(Entity, entity_id)
        if previous is None or entity is None or entity.status not in {"active", "provisional"}:
            raise DocketError(
                code="entity_resolution_not_found",
                message="The resolution or replacement entity was not found.",
            )
        if previous.entity_class != entity.entity_class:
            raise DocketError(
                code="entity_class_mismatch",
                message="A resolution can only be rebound within its entity class.",
            )
        correction = EntityResolution(
            entity_class=previous.entity_class,
            mention=previous.mention,
            normalized_mention=previous.normalized_mention,
            state="resolved" if entity.status == "active" else "provisional",
            resolved_entity_id=entity.id,
            candidate_entity_ids=[str(entity.id)],
            source_item_id=previous.source_item_id,
            semantic_candidate_id=previous.semantic_candidate_id,
        )
        self.session.add(correction)
        self.session.flush()
        previous.corrected_resolution_id = correction.id
        self.session.add(
            AuditEvent(
                event_type="entity_resolution.corrected",
                entity_type="entity_resolution",
                entity_id=correction.id,
                actor_type="hermes",
                actor_id=actor_id,
                data={
                    "previous_resolution_id": str(previous.id),
                    "resolved_entity_id": str(entity.id),
                    "authority": IntentAuthority.EXPLICIT_USER.value,
                },
            )
        )
        return EntityResolutionResult(
            resolution_id=correction.id,
            entity_class=entity.entity_class,
            mention=correction.mention,
            state=correction.state,
            resolved_entity=serialize_entity(entity),
            candidates=[serialize_entity(entity)],
        )

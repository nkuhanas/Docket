from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.domain.enums import IntentAuthority
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CanonicalEvent,
    Entity,
    EntityAlias,
    EntityRelation,
    EntityResolution,
    OutboxEvent,
    QueueItem,
    SemanticCandidate,
)
from docket.models.base import utc_now
from docket.schemas.entities import (
    EntityAttributeKey,
    EntityAttributes,
    EntityPredicate,
    EntityRelationAttributes,
    EntityRelationDirection,
    EntityResolutionResult,
    EntityResult,
    EntitySearchStatus,
    EntitySnapshotResult,
)
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


def _attribute_dict(attributes: EntityAttributes | dict[str, Any] | None) -> dict[str, Any]:
    if attributes is None:
        return {}
    model = (
        attributes
        if isinstance(attributes, EntityAttributes)
        else EntityAttributes.model_validate(attributes)
    )
    return model.model_dump(mode="json", exclude_none=True)


def _relation_attribute_dict(
    attributes: EntityRelationAttributes | dict[str, Any] | None,
) -> dict[str, Any]:
    if attributes is None:
        return {}
    model = (
        attributes
        if isinstance(attributes, EntityRelationAttributes)
        else EntityRelationAttributes.model_validate(attributes)
    )
    return model.model_dump(mode="json", exclude_none=True)


class EntityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _validate_operator_identity(
        self,
        *,
        entity_class: EntityClass,
        attributes: dict[str, Any],
        entity_id: uuid.UUID | None = None,
    ) -> None:
        if not attributes.get("is_operator"):
            return
        if entity_class != "person":
            raise DocketError(
                code="invalid_operator_entity",
                message="Only a person entity can represent the Docket operator.",
            )
        existing = self.session.scalars(
            select(Entity).where(
                Entity.entity_class == "person",
                Entity.status.in_(("active", "provisional")),
            )
        ).all()
        duplicate = next(
            (
                candidate
                for candidate in existing
                if candidate.id != entity_id and candidate.attributes.get("is_operator") is True
            ),
            None,
        )
        if duplicate is not None:
            raise DocketError(
                code="operator_entity_exists",
                message="Docket already has an active operator person identity.",
                details={"entity_id": str(duplicate.id)},
            )

    def _matches(self, entity_class: EntityClass, normalized: str) -> list[Entity]:
        corrected_ids = select(EntityResolution.corrected_resolution_id).where(
            EntityResolution.entity_class == entity_class,
            EntityResolution.normalized_mention == normalized,
            EntityResolution.corrected_resolution_id.is_not(None),
        )
        correction = self.session.scalar(
            select(EntityResolution)
            .where(EntityResolution.id.in_(corrected_ids))
            .order_by(EntityResolution.created_at.desc(), EntityResolution.id.desc())
            .limit(1)
        )
        if correction is not None and correction.resolved_entity_id is not None:
            corrected_entity = self.session.get(Entity, correction.resolved_entity_id)
            if corrected_entity is not None and corrected_entity.status in {
                "active",
                "provisional",
            }:
                return [corrected_entity]
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

    @staticmethod
    def _resolved_candidate_fields(
        candidate: SemanticCandidate,
        *,
        previous_resolution_id: uuid.UUID,
        resolution_id: uuid.UUID,
        entity: Entity,
    ) -> None:
        raw_resolutions = candidate.fields.get("entity_resolutions")
        if not isinstance(raw_resolutions, list):
            return
        changed = False
        resolutions: list[dict[str, Any]] = []
        for raw in raw_resolutions:
            if not isinstance(raw, dict):
                continue
            resolution = dict(raw)
            if resolution.get("resolution_id") == str(previous_resolution_id):
                resolution.update(
                    {
                        "state": "resolved",
                        "resolution_id": str(resolution_id),
                        "entity_id": str(entity.id),
                        "canonical_name": entity.canonical_name,
                        "candidate_entity_ids": [str(entity.id)],
                    }
                )
                changed = True
            resolutions.append(resolution)
        if changed:
            candidate.fields = {**candidate.fields, "entity_resolutions": resolutions}
            candidate.version += 1

    def _resume_candidate_if_ascertained(self, candidate: SemanticCandidate) -> None:
        if candidate.status != "needs_clarification" or not isinstance(candidate.resolution, dict):
            return
        if candidate.resolution.get("reason") != "entity_resolution_required":
            return
        raw_resolutions = candidate.fields.get("entity_resolutions")
        resolutions = raw_resolutions if isinstance(raw_resolutions, list) else []
        unresolved = [
            resolution
            for resolution in resolutions
            if isinstance(resolution, dict)
            and resolution.get("required", True)
            and resolution.get("state") != "resolved"
        ]
        if unresolved:
            return
        queue_item = (
            self.session.get(QueueItem, candidate.queue_item_id)
            if candidate.queue_item_id is not None
            else None
        )
        if queue_item is not None and queue_item.status == "pending":
            queue_item.status = "completed"
            queue_item.resolved_at = utc_now()
            queue_item.resolution_code = "clarification_resolved"
            queue_item.version += 1
            self.session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=(
                        f"discord_projection:{queue_item.id}:clarification-resolved:"
                        f"v{queue_item.version}"
                    ),
                    payload={"queue_item_id": str(queue_item.id)},
                    status="pending",
                )
            )
        candidate.status = "pending"
        candidate.queue_item_id = None
        candidate.next_attempt_at = utc_now()
        candidate.resolution = {"disposition": "entity_resolution_completed"}
        candidate.version += 1

    def _resolve_waiting_mentions(
        self,
        entity: Entity,
        *,
        normalized_mentions: set[str],
    ) -> None:
        """Resume formulations waiting on an entity the operator just established."""
        if entity.status != "active" or not normalized_mentions:
            return
        waiting = self.session.scalars(
            select(EntityResolution).where(
                EntityResolution.entity_class == entity.entity_class,
                EntityResolution.normalized_mention.in_(normalized_mentions),
                EntityResolution.state.in_(("unresolved", "provisional")),
                or_(
                    EntityResolution.resolved_entity_id.is_(None),
                    EntityResolution.resolved_entity_id == entity.id,
                ),
            )
        ).all()
        for resolution in waiting:
            resolution.state = "resolved"
            resolution.resolved_entity_id = entity.id
            resolution.candidate_entity_ids = [str(entity.id)]
            if resolution.semantic_candidate_id is None:
                continue
            candidate = self.session.get(SemanticCandidate, resolution.semantic_candidate_id)
            if candidate is None:
                continue
            self._resolved_candidate_fields(
                candidate,
                previous_resolution_id=resolution.id,
                resolution_id=resolution.id,
                entity=entity,
            )
            self._resume_candidate_if_ascertained(candidate)

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

    def relationships(self, entity_id: uuid.UUID, *, limit: int = 100) -> list[dict[str, Any]]:
        entity = self.session.get(Entity, entity_id)
        if entity is None or entity.status not in {"active", "provisional"}:
            raise DocketError(code="entity_not_found", message="Active entity was not found.")
        relations = self.session.scalars(
            select(EntityRelation)
            .where(
                EntityRelation.status == "active",
                or_(
                    EntityRelation.subject_entity_id == entity_id,
                    EntityRelation.object_entity_id == entity_id,
                ),
            )
            .order_by(EntityRelation.predicate, EntityRelation.id)
            .limit(limit)
        ).all()
        results: list[dict[str, Any]] = []
        for relation in relations:
            subject = self.session.get(Entity, relation.subject_entity_id)
            object_ = self.session.get(Entity, relation.object_entity_id)
            if subject is None or object_ is None:
                continue
            results.append(
                {
                    "relation_id": str(relation.id),
                    "predicate": relation.predicate,
                    "subject": serialize_entity(subject).model_dump(mode="json"),
                    "object": serialize_entity(object_).model_dump(mode="json"),
                    "attributes": dict(relation.attributes),
                    "authority": relation.authority,
                    "version": relation.version,
                }
            )
        return results

    def snapshot(self, entity_id: uuid.UUID) -> EntitySnapshotResult:
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            raise DocketError(code="entity_not_found", message="Entity was not found.")
        aliases = self.session.scalars(
            select(EntityAlias)
            .where(EntityAlias.entity_id == entity_id)
            .order_by(EntityAlias.alias, EntityAlias.id)
            .limit(100)
        ).all()
        return EntitySnapshotResult.model_validate(
            {
                "entity": serialize_entity(entity).model_dump(mode="json"),
                "aliases": [
                    {
                        "alias_id": alias.id,
                        "alias": alias.alias,
                        "authority": alias.authority,
                        "confidence": alias.confidence,
                    }
                    for alias in aliases
                ],
                "relationships": self.relationships(entity_id, limit=100)
                if entity.status in {"active", "provisional"}
                else [],
            }
        )

    def search(
        self,
        *,
        query: str | None = None,
        entity_class: EntityClass | None = None,
        status: EntitySearchStatus = "active_or_provisional",
        predicate: EntityPredicate | None = None,
        related_entity_id: uuid.UUID | None = None,
        direction: EntityRelationDirection = "any",
        is_operator: bool | None = None,
        limit: int = 20,
    ) -> list[EntitySnapshotResult]:
        statement = select(Entity)
        if entity_class is not None:
            statement = statement.where(Entity.entity_class == entity_class)
        if status == "active_or_provisional":
            statement = statement.where(Entity.status.in_(("active", "provisional")))
        elif status != "all":
            statement = statement.where(Entity.status == status)

        relations = self.session.scalars(
            select(EntityRelation).where(EntityRelation.status == "active")
        ).all()
        if predicate is not None:
            relations = [relation for relation in relations if relation.predicate == predicate]
        if related_entity_id is not None:
            relations = [
                relation
                for relation in relations
                if related_entity_id
                in (relation.subject_entity_id, relation.object_entity_id)
            ]
        if predicate is not None or related_entity_id is not None:
            if direction == "subject":
                related_ids = {relation.subject_entity_id for relation in relations}
            elif direction == "object":
                related_ids = {relation.object_entity_id for relation in relations}
            else:
                related_ids = {
                    entity_id
                    for relation in relations
                    for entity_id in (relation.subject_entity_id, relation.object_entity_id)
                }
            statement = statement.where(Entity.id.in_(related_ids))

        entities = list(self.session.scalars(statement.order_by(Entity.canonical_name, Entity.id)))
        alias_rows = self.session.scalars(select(EntityAlias)).all()
        aliases: dict[uuid.UUID, list[EntityAlias]] = {}
        for alias in alias_rows:
            aliases.setdefault(alias.entity_id, []).append(alias)
        normalized_query = normalize_entity_name(query) if query else None
        matched: list[tuple[int, Entity]] = []
        for entity in entities:
            if (
                is_operator is not None
                and entity.attributes.get("is_operator") is not is_operator
            ):
                continue
            rank = 2
            if normalized_query:
                alias_names = [alias.normalized_alias for alias in aliases.get(entity.id, [])]
                metadata = normalize_entity_name(
                    json.dumps(entity.attributes, ensure_ascii=False, sort_keys=True)
                )
                if entity.normalized_name == normalized_query or normalized_query in alias_names:
                    rank = 0
                elif normalized_query in entity.normalized_name or any(
                    normalized_query in alias for alias in alias_names
                ):
                    rank = 1
                elif normalized_query not in metadata:
                    continue
            matched.append((rank, entity))
        matched.sort(key=lambda item: (item[0], item[1].canonical_name.casefold(), str(item[1].id)))
        return [self.snapshot(entity.id) for _, entity in matched[:limit]]

    def create(
        self,
        *,
        entity_class: EntityClass,
        canonical_name: str,
        attributes: EntityAttributes | dict[str, Any],
        authority: IntentAuthority,
        provisional: bool = False,
        actor_type: str = "docket",
        actor_id: str | None = None,
    ) -> EntityResult:
        attribute_values = _attribute_dict(attributes)
        normalized = normalize_entity_name(canonical_name)
        if not normalized:
            raise DocketError(code="invalid_entity_name", message="Entity name cannot be empty.")
        existing = self.session.scalar(
            select(Entity).where(
                Entity.entity_class == entity_class,
                Entity.normalized_name == normalized,
                Entity.status.in_(("active", "provisional")),
            )
        )
        if existing is not None:
            self._validate_operator_identity(
                entity_class=entity_class,
                attributes={**existing.attributes, **attribute_values},
                entity_id=existing.id,
            )
            if (
                existing.status == "provisional"
                and not provisional
                and authority != IntentAuthority.INFERRED
            ):
                existing.status = "active"
                existing.authority = authority.value
                merged_attributes = {**existing.attributes, **attribute_values}
                self._validate_operator_identity(
                    entity_class=entity_class,
                    attributes=merged_attributes,
                    entity_id=existing.id,
                )
                existing.attributes = merged_attributes
                existing.version += 1
            elif attribute_values and any(
                existing.attributes.get(key) != value
                for key, value in attribute_values.items()
            ):
                raise DocketError(
                    code="entity_exists",
                    message=(
                        "The canonical entity already exists with different metadata; "
                        "read and update it explicitly."
                    ),
                    details={
                        "entity_id": str(existing.id),
                        "current_version": existing.version,
                    },
                )
            self._resolve_waiting_mentions(existing, normalized_mentions={normalized})
            return serialize_entity(existing)
        self._validate_operator_identity(
            entity_class=entity_class,
            attributes=attribute_values,
        )
        entity = Entity(
            entity_class=entity_class,
            canonical_name=canonical_name.strip(),
            normalized_name=normalized,
            status="provisional" if provisional else "active",
            attributes=attribute_values,
            authority=authority.value,
        )
        self.session.add(entity)
        self.session.flush()
        self._resolve_waiting_mentions(entity, normalized_mentions={normalized})
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
        attribute_updates: EntityAttributes | dict[str, Any] | None,
        remove_attribute_keys: list[EntityAttributeKey] | None,
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
        if canonical_name is None and attribute_updates is None and not remove_attribute_keys:
            raise DocketError(
                code="empty_entity_update",
                message="Entity update must change a name or at least one attribute.",
            )
        if canonical_name is not None:
            normalized_name = normalize_entity_name(canonical_name)
            if not normalized_name:
                raise DocketError(
                    code="invalid_entity_name", message="Entity name cannot be empty."
                )
            duplicate = self.session.scalar(
                select(Entity).where(
                    Entity.id != entity.id,
                    Entity.entity_class == entity.entity_class,
                    Entity.normalized_name == normalized_name,
                    Entity.status.in_(("active", "provisional")),
                )
            )
            if duplicate is not None:
                raise DocketError(
                    code="entity_identity_conflict",
                    message="Another active entity already has that canonical identity.",
                    details={"entity_id": str(duplicate.id)},
                )
            entity.canonical_name = canonical_name.strip()
            entity.normalized_name = normalized_name
        updates = _attribute_dict(attribute_updates)
        removals = set(remove_attribute_keys or [])
        overlap = removals.intersection(updates)
        if overlap:
            raise DocketError(
                code="invalid_entity_attribute_patch",
                message="An attribute cannot be both updated and removed.",
                details={"keys": sorted(overlap)},
            )
        patched_attributes = {
            key: value for key, value in entity.attributes.items() if key not in removals
        }
        patched_attributes.update(updates)
        EntityAttributes.model_validate(patched_attributes)
        self._validate_operator_identity(
            entity_class=cast(EntityClass, entity.entity_class),
            attributes=patched_attributes,
            entity_id=entity.id,
        )
        if attribute_updates is not None or removals:
            entity.attributes = patched_attributes
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
        if not normalized:
            raise DocketError(code="invalid_entity_alias", message="Entity alias cannot be empty.")
        if not 0.0 <= confidence <= 1.0:
            raise DocketError(
                code="invalid_entity_alias_confidence",
                message="Entity alias confidence must be between 0 and 1.",
            )
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
        self._resolve_waiting_mentions(entity, normalized_mentions={normalized})
        return serialize_entity(entity)

    def relate(
        self,
        *,
        subject_entity_id: uuid.UUID,
        predicate: EntityPredicate,
        object_entity_id: uuid.UUID,
        authority: IntentAuthority,
        attributes: EntityRelationAttributes | dict[str, Any] | None = None,
        actor_id: str | None = None,
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
        attribute_values = _relation_attribute_dict(attributes)
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
                attributes=attribute_values,
            )
            self.session.add(relation)
            self.session.flush()
        elif relation.status == "retracted":
            relation.status = "active"
            relation.authority = authority.value
            relation.attributes = attribute_values
            relation.version += 1
        elif relation.attributes != attribute_values:
            raise DocketError(
                code="entity_relation_exists",
                message=(
                    "The relationship already exists with different metadata; "
                    "update it explicitly."
                ),
                details={"relation_id": str(relation.id), "current_version": relation.version},
            )
        self.session.add(
            AuditEvent(
                event_type="entity_relation.created",
                entity_type="entity_relation",
                entity_id=relation.id,
                actor_type="hermes" if actor_id else "docket",
                actor_id=actor_id,
                data={"predicate": predicate, "version": relation.version},
            )
        )
        return relation.id

    def update_relation(
        self,
        *,
        relation_id: uuid.UUID,
        expected_version: int,
        attributes: EntityRelationAttributes | dict[str, Any],
        authority: IntentAuthority,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        relation = self.session.scalar(
            select(EntityRelation).where(EntityRelation.id == relation_id).with_for_update()
        )
        if relation is None or relation.status != "active":
            raise DocketError(
                code="entity_relation_not_found",
                message="Active entity relationship was not found.",
            )
        if relation.version != expected_version:
            raise DocketError(
                code="entity_relation_version_changed",
                message="The relationship changed after it was read.",
                details={"current_version": relation.version},
            )
        relation.attributes = _relation_attribute_dict(attributes)
        relation.authority = authority.value
        relation.version += 1
        self.session.add(
            AuditEvent(
                event_type="entity_relation.updated",
                entity_type="entity_relation",
                entity_id=relation.id,
                actor_type="hermes" if actor_id else "docket",
                actor_id=actor_id,
                data={"version": relation.version},
            )
        )
        return next(
            item
            for item in self.relationships(relation.subject_entity_id)
            if item["relation_id"] == str(relation.id)
        )

    def retract_relation(
        self,
        *,
        relation_id: uuid.UUID,
        expected_version: int,
        reason: str,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        relation = self.session.scalar(
            select(EntityRelation).where(EntityRelation.id == relation_id).with_for_update()
        )
        if relation is None:
            raise DocketError(
                code="entity_relation_not_found", message="Entity relationship was not found."
            )
        if relation.version != expected_version:
            raise DocketError(
                code="entity_relation_version_changed",
                message="The relationship changed after it was read.",
                details={"current_version": relation.version},
            )
        if relation.status != "retracted":
            relation.status = "retracted"
            relation.version += 1
            self.session.add(
                AuditEvent(
                    event_type="entity_relation.retracted",
                    entity_type="entity_relation",
                    entity_id=relation.id,
                    actor_type="hermes" if actor_id else "docket",
                    actor_id=actor_id,
                    data={"reason": reason, "version": relation.version},
                )
            )
        return {
            "relation_id": str(relation.id),
            "status": relation.status,
            "version": relation.version,
        }

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
        relations = self.session.scalars(
            select(EntityRelation).where(
                EntityRelation.status == "active",
                or_(
                    EntityRelation.subject_entity_id == absorbed.id,
                    EntityRelation.object_entity_id == absorbed.id,
                ),
            )
        ).all()
        for relation in relations:
            subject_id = (
                survivor.id
                if relation.subject_entity_id == absorbed.id
                else relation.subject_entity_id
            )
            object_id = (
                survivor.id
                if relation.object_entity_id == absorbed.id
                else relation.object_entity_id
            )
            relation.status = "retracted"
            relation.version += 1
            if subject_id == object_id:
                continue
            self.relate(
                subject_entity_id=subject_id,
                predicate=cast(EntityPredicate, relation.predicate),
                object_entity_id=object_id,
                authority=authority,
                attributes=dict(relation.attributes),
            )
        resolutions = self.session.scalars(
            select(EntityResolution).where(EntityResolution.resolved_entity_id == absorbed.id)
        ).all()
        for resolution in resolutions:
            resolution.resolved_entity_id = survivor.id
            resolution.candidate_entity_ids = [
                str(survivor.id) if value == str(absorbed.id) else value
                for value in resolution.candidate_entity_ids
            ]
            if resolution.semantic_candidate_id is not None:
                candidate = self.session.get(SemanticCandidate, resolution.semantic_candidate_id)
                if candidate is not None:
                    self._resolved_candidate_fields(
                        candidate,
                        previous_resolution_id=resolution.id,
                        resolution_id=resolution.id,
                        entity=survivor,
                    )
                    self._resume_candidate_if_ascertained(candidate)
        events = self.session.scalars(
            select(CanonicalEvent).where(CanonicalEvent.entity_refs != [])
        ).all()
        for event in events:
            changed = False
            entity_refs: list[dict[str, Any]] = []
            for raw in event.entity_refs:
                entity_ref = dict(raw)
                if entity_ref.get("entity_id") == str(absorbed.id):
                    entity_ref.update(
                        {
                            "entity_id": str(survivor.id),
                            "entity_class": survivor.entity_class,
                            "canonical_name": survivor.canonical_name,
                        }
                    )
                    changed = True
                entity_refs.append(entity_ref)
            if changed:
                event.entity_refs = entity_refs
                event.version += 1
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
        if previous.semantic_candidate_id is not None:
            candidate = self.session.get(SemanticCandidate, previous.semantic_candidate_id)
            if candidate is not None:
                self._resolved_candidate_fields(
                    candidate,
                    previous_resolution_id=previous.id,
                    resolution_id=correction.id,
                    entity=entity,
                )
                self._resume_candidate_if_ascertained(candidate)
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

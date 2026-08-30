from __future__ import annotations

import json
from collections import deque
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    Affiliation,
    CanonicalEvent,
    Entity,
    EntityAlias,
    EventItemLink,
    Fact,
    IdentityHandle,
    Interaction,
    InteractionParticipant,
    Item,
    OrganizationInstitutionProfile,
    PersonProfile,
    Relationship,
    Task,
    TemporalBinding,
)
from docket.models.base import utc_now
from docket.services.registry import normalize_registry_text


class NetworkQueryService:
    """Bounded query-oriented projections over the registered context graph."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _entity(self, ref_id: str, *, kinds: set[str] | None = None) -> Entity:
        entity = self.session.scalar(
            select(Entity).where(
                Entity.ref_id == ref_id,
                Entity.canonical_status == "active",
            )
        )
        if entity is None or (kinds is not None and entity.entity_kind not in kinds):
            raise DocketError(
                code="registered_entity_not_found",
                message="A requested registered Entity was not found.",
                details={"entity_ref": ref_id},
            )
        return entity

    def _entity_by_id(self, entity_id: Any) -> Entity | None:
        entity = self.session.get(Entity, entity_id)
        if entity is None or entity.canonical_status != "active":
            return None
        return entity

    @staticmethod
    def _page(items: list[dict[str, Any]], *, limit: int, cursor: str | None) -> dict[str, Any]:
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise DocketError(
                code="invalid_cursor", message="Cursor must be a non-negative offset."
            ) from exc
        if offset < 0:
            raise DocketError(
                code="invalid_cursor", message="Cursor must be a non-negative offset."
            )
        bounded = items[offset : offset + limit]
        next_offset = offset + len(bounded)
        envelope: dict[str, Any] = {
            "ok": True,
            "items": bounded,
            "count": len(bounded),
            "total_if_known": len(items),
            "truncated": next_offset < len(items),
            **({"cursor": str(next_offset)} if next_offset < len(items) else {}),
        }
        while bounded and len(json.dumps(envelope, separators=(",", ":")).encode()) > 16384:
            bounded.pop()
            next_offset = offset + len(bounded)
            envelope["count"] = len(bounded)
            envelope["truncated"] = True
            envelope["cursor"] = str(next_offset)
        return envelope

    @staticmethod
    def _compact_value(value: Any) -> Any:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= 512:
                return value
            return encoded[:509].decode("utf-8", errors="ignore") + "…"
        if isinstance(value, dict):
            return {
                str(key): NetworkQueryService._compact_value(nested)
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [NetworkQueryService._compact_value(item) for item in value]
        return value

    @classmethod
    def _bounded_context(cls, payload: dict[str, Any]) -> dict[str, Any]:
        bounded = cls._compact_value(payload)
        if not isinstance(bounded, dict):
            raise TypeError("Context projection must remain an object")
        trimmed: set[str] = set()

        def collections(value: Any, path: str = "") -> list[tuple[str, list[Any]]]:
            found: list[tuple[str, list[Any]]] = []
            if isinstance(value, dict):
                for key, nested in value.items():
                    found.extend(collections(nested, f"{path}.{key}" if path else key))
            elif isinstance(value, list):
                found.append((path, value))
            return found

        while len(json.dumps(bounded, separators=(",", ":")).encode()) > 16384:
            candidates = [item for item in collections(bounded) if item[1]]
            if not candidates:
                raise DocketError(
                    code="network_output_budget_exceeded",
                    message="A compact graph projection could not fit its byte budget.",
                )
            path, values = max(candidates, key=lambda item: len(item[1]))
            values.pop()
            trimmed.add(path)
        if trimmed:
            bounded["warnings"] = [f"output_truncated:{path}" for path in sorted(trimmed)]
        return bounded

    def _handles(self, entity: Entity) -> list[IdentityHandle]:
        return list(
            self.session.scalars(
                select(IdentityHandle)
                .where(
                    IdentityHandle.entity_id == entity.id,
                    IdentityHandle.status.in_(("bound", "historical")),
                )
                .order_by(IdentityHandle.handle_type, IdentityHandle.normalized_value)
            )
        )

    def _relationship_summary(self, entity: Entity) -> str:
        relationships = list(
            self.session.scalars(
                select(Relationship).where(
                    Relationship.status == "active",
                    or_(
                        Relationship.subject_entity_id == entity.id,
                        Relationship.object_entity_id == entity.id,
                    ),
                )
            )
        )
        labels = [
            item.relationship_type or item.context or "contextual relationship"
            for item in relationships[:3]
        ]
        return ", ".join(labels) if labels else "No current relationship recorded"

    def network_search(
        self,
        *,
        query: str,
        entity_kinds: list[str] | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        normalized = normalize_registry_text(query)
        statement = select(Entity).where(
            Entity.canonical_status == "active",
        )
        if entity_kinds:
            statement = statement.where(Entity.entity_kind.in_(entity_kinds))
        direct = list(
            self.session.scalars(
                statement.where(Entity.normalized_name.contains(normalized)).order_by(
                    Entity.display_name
                )
            )
        )
        aliases = list(
            self.session.scalars(
                select(EntityAlias).where(EntityAlias.normalized_alias.contains(normalized))
            )
        )
        by_id = {entity.id: entity for entity in direct}
        for alias in aliases:
            entity = self._entity_by_id(alias.entity_id)
            if entity is not None and (not entity_kinds or entity.entity_kind in entity_kinds):
                by_id[entity.id] = entity
        ranked = sorted(
            by_id.values(),
            key=lambda item: (
                0 if item.normalized_name == normalized else 1,
                item.display_name.casefold(),
            ),
        )
        projected = []
        for entity in ranked:
            handles = self._handles(entity)
            projected.append(
                {
                    "ref": entity.ref_id,
                    "type": entity.entity_kind,
                    "display_name": entity.display_name,
                    "identity_summary": ", ".join(
                        f"{item.handle_type}:{item.value}" for item in handles[:3]
                    )
                    or "No bound handles",
                    "relationship_summary": self._relationship_summary(entity),
                }
            )
        return self._page(projected, limit=limit, cursor=cursor)

    def _affiliation_projection(self, item: Affiliation) -> dict[str, Any]:
        organization = self._entity_by_id(item.organization_entity_id)
        return self._bounded_context(
            {
                "ref": item.ref_id,
                "organization": (
                    {
                        "ref": organization.ref_id,
                        "name": organization.display_name,
                        "type": organization.entity_kind,
                    }
                    if organization is not None
                    else None
                ),
                "role": item.role,
                "domain": item.domain,
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "status": item.status,
                "basis_refs": item.basis_refs,
            }
        )

    def _relationship_projection(
        self, item: Relationship, *, perspective: Entity
    ) -> dict[str, Any]:
        counterpart_id = (
            item.object_entity_id
            if item.subject_entity_id == perspective.id
            else item.subject_entity_id
        )
        counterpart = self._entity_by_id(counterpart_id)
        return self._bounded_context(
            {
                "ref": item.ref_id,
                "counterpart": (
                    {
                        "ref": counterpart.ref_id,
                        "name": counterpart.display_name,
                        "type": counterpart.entity_kind,
                    }
                    if counterpart is not None
                    else None
                ),
                "direction": (
                    "outgoing" if item.subject_entity_id == perspective.id else "incoming"
                ),
                "relationship_type": item.relationship_type,
                "context": item.context,
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "status": item.status,
                "basis_refs": item.basis_refs,
            }
        )

    def _recent_interactions(self, entity: Entity, *, limit: int = 10) -> list[dict[str, Any]]:
        ids = select(InteractionParticipant.interaction_id).where(
            InteractionParticipant.entity_id == entity.id
        )
        interactions = list(
            self.session.scalars(
                select(Interaction)
                .where(Interaction.id.in_(ids), Interaction.status != "retracted")
                .order_by(Interaction.occurred_at.desc())
                .limit(limit)
            )
        )
        return [
            {
                "ref": item.ref_id,
                "type": item.interaction_type,
                "occurred_at": item.occurred_at.isoformat(),
                "summary": item.summary,
                "event_ref": item.event_ref,
                "organization_refs": item.organization_refs,
            }
            for item in interactions
        ]

    def person_context(self, person_ref: str) -> dict[str, Any]:
        person = self._entity(person_ref, kinds={"person"})
        profile = self.session.get(PersonProfile, person.id)
        affiliations = list(
            self.session.scalars(
                select(Affiliation)
                .where(Affiliation.subject_entity_id == person.id)
                .order_by(Affiliation.status, Affiliation.created_at.desc())
            )
        )
        relationships = list(
            self.session.scalars(
                select(Relationship)
                .where(
                    or_(
                        Relationship.subject_entity_id == person.id,
                        Relationship.object_entity_id == person.id,
                    )
                )
                .order_by(Relationship.status, Relationship.created_at.desc())
            )
        )
        facts = list(
            self.session.scalars(
                select(Fact)
                .where(Fact.subject_ref == person.ref_id)
                .order_by(Fact.status, Fact.predicate, Fact.created_at.desc())
            )
        )
        return {
            "ok": True,
            "ref": person.ref_id,
            "type": "person",
            "display_name": person.display_name,
            "preferred_name": profile.preferred_name if profile else None,
            "pronouns": profile.pronouns if profile else None,
            "is_operator": bool(profile and profile.is_operator),
            "handles": [
                {
                    "ref": item.ref_id,
                    "type": item.handle_type,
                    "value": item.value,
                    "status": item.status,
                }
                for item in self._handles(person)
            ],
            "current_affiliations": [
                self._affiliation_projection(item)
                for item in affiliations
                if item.status == "active"
            ],
            "historical_affiliations": [
                self._affiliation_projection(item)
                for item in affiliations
                if item.status != "active"
            ],
            "relationships": [
                self._relationship_projection(item, perspective=person) for item in relationships
            ],
            "current_facts": [
                {
                    "ref": item.ref_id,
                    "predicate": item.predicate,
                    "value": item.value_json,
                    "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                    "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                    "basis_refs": item.basis_refs,
                }
                for item in facts
                if item.status == "active"
            ],
            "historical_facts": [
                {
                    "ref": item.ref_id,
                    "predicate": item.predicate,
                    "value": item.value_json,
                    "status": item.status,
                    "basis_refs": item.basis_refs,
                }
                for item in facts
                if item.status != "active"
            ],
            "recent_interactions": self._recent_interactions(person),
            "provenance_refs": list(
                dict.fromkeys(
                    [
                        *person.basis_refs,
                        *[ref_id for item in affiliations for ref_id in item.basis_refs],
                        *[ref_id for item in relationships for ref_id in item.basis_refs],
                        *[ref_id for item in facts for ref_id in item.basis_refs],
                    ]
                )
            )[:50],
        }

    def organization_context(self, organization_ref: str) -> dict[str, Any]:
        organization = self._entity(organization_ref, kinds={"organization", "institution"})
        profile = self.session.get(OrganizationInstitutionProfile, organization.id)
        parent = (
            self._entity_by_id(profile.parent_entity_id)
            if profile is not None and profile.parent_entity_id is not None
            else None
        )
        children = list(
            self.session.scalars(
                select(OrganizationInstitutionProfile).where(
                    OrganizationInstitutionProfile.parent_entity_id == organization.id
                )
            )
        )
        affiliations = list(
            self.session.scalars(
                select(Affiliation).where(
                    Affiliation.organization_entity_id == organization.id,
                    Affiliation.status == "active",
                )
            )
        )
        interactions = [
            item
            for item in self.session.scalars(
                select(Interaction)
                .where(Interaction.status != "retracted")
                .order_by(Interaction.occurred_at.desc())
                .limit(100)
            )
            if organization.ref_id in item.organization_refs
        ][:10]
        return {
            "ok": True,
            "ref": organization.ref_id,
            "type": organization.entity_kind,
            "display_name": organization.display_name,
            "organization_type": profile.organization_type if profile else None,
            "description": profile.description if profile else None,
            "hierarchy": {
                "parent": (
                    {"ref": parent.ref_id, "name": parent.display_name}
                    if parent is not None
                    else None
                ),
                "children": [
                    {
                        "ref": child_entity.ref_id,
                        "name": child_entity.display_name,
                        "type": child_entity.entity_kind,
                    }
                    for child in children
                    if (child_entity := self._entity_by_id(child.entity_id)) is not None
                ][:25],
            },
            "known_people": [
                {
                    "ref": person.ref_id,
                    "name": person.display_name,
                    "affiliation_ref": item.ref_id,
                    "role": item.role,
                    "domain": item.domain,
                }
                for item in affiliations
                if (person := self._entity_by_id(item.subject_entity_id)) is not None
            ][:25],
            "recent_interactions": [
                {
                    "ref": item.ref_id,
                    "occurred_at": item.occurred_at.isoformat(),
                    "summary": item.summary,
                }
                for item in interactions
            ],
            "events": [],
            "calendar_routing": {"rules": [], "precedent": []},
            "provenance_refs": list(
                dict.fromkeys(
                    [
                        *organization.basis_refs,
                        *[ref_id for item in affiliations for ref_id in item.basis_refs],
                    ]
                )
            )[:50],
        }

    def query_people(
        self,
        *,
        affiliated_with: str | None,
        shares_course_with_operator: bool,
        known_through: str | None,
        relationship_type: str | None,
        current_role: str | None,
        interaction_recency_days: int | None,
        fact_constraints: dict[str, Any],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        people = list(
            self.session.scalars(
                select(Entity).where(
                    Entity.entity_kind == "person",
                    Entity.canonical_status == "active",
                )
            )
        )
        organization = (
            self._entity(affiliated_with, kinds={"organization", "institution"})
            if affiliated_with is not None
            else None
        )
        cutoff = (
            utc_now() - timedelta(days=interaction_recency_days)
            if interaction_recency_days is not None
            else None
        )
        operator_courses: set[str] = set()
        if shares_course_with_operator:
            operator_profile = self.session.scalar(
                select(PersonProfile).where(PersonProfile.is_operator.is_(True))
            )
            if operator_profile is None:
                raise DocketError(
                    code="operator_entity_not_found",
                    message="Course-sharing queries require the canonical Operator Person.",
                )
            operator_entity_ref = self.session.scalar(
                select(Entity.ref_id).where(Entity.id == operator_profile.entity_id)
            )
            operator_course_facts = self.session.scalars(
                select(Fact).where(
                    Fact.subject_ref == operator_entity_ref,
                    Fact.status == "active",
                    Fact.predicate.in_(("course", "course_ref", "course_section")),
                )
            )
            operator_courses = {str(item.value_json) for item in operator_course_facts}
        matches: list[dict[str, Any]] = []
        for person in people:
            affiliations = list(
                self.session.scalars(
                    select(Affiliation).where(
                        Affiliation.subject_entity_id == person.id,
                        Affiliation.status == "active",
                    )
                )
            )
            if organization is not None and not any(
                item.organization_entity_id == organization.id for item in affiliations
            ):
                continue
            if current_role is not None and not any(
                item.role
                and normalize_registry_text(current_role) in normalize_registry_text(item.role)
                for item in affiliations
            ):
                continue
            relationships = list(
                self.session.scalars(
                    select(Relationship).where(
                        Relationship.status == "active",
                        or_(
                            Relationship.subject_entity_id == person.id,
                            Relationship.object_entity_id == person.id,
                        ),
                    )
                )
            )
            if relationship_type is not None and not any(
                item.relationship_type == relationship_type for item in relationships
            ):
                continue
            if known_through is not None and not any(
                item.context
                and normalize_registry_text(known_through) in normalize_registry_text(item.context)
                for item in relationships
            ):
                continue
            facts = list(
                self.session.scalars(
                    select(Fact).where(
                        Fact.subject_ref == person.ref_id,
                        Fact.status == "active",
                    )
                )
            )
            fact_map = {item.predicate: item.value_json for item in facts}
            if shares_course_with_operator and not operator_courses.intersection(
                str(item.value_json)
                for item in facts
                if item.predicate in {"course", "course_ref", "course_section"}
            ):
                continue
            if any(fact_map.get(key) != value for key, value in fact_constraints.items()):
                continue
            if cutoff is not None:
                recent = self.session.scalar(
                    select(Interaction.id)
                    .join(
                        InteractionParticipant,
                        InteractionParticipant.interaction_id == Interaction.id,
                    )
                    .where(
                        InteractionParticipant.entity_id == person.id,
                        Interaction.occurred_at >= cutoff,
                    )
                    .limit(1)
                )
                if recent is None:
                    continue
            matches.append(
                {
                    "ref": person.ref_id,
                    "display_name": person.display_name,
                    "affiliation_refs": [item.ref_id for item in affiliations],
                    "relationship_refs": [item.ref_id for item in relationships],
                    "matched_facts": {
                        key: fact_map[key] for key in fact_constraints if key in fact_map
                    },
                }
            )
        matches.sort(key=lambda item: item["display_name"].casefold())
        return self._page(matches, limit=limit, cursor=cursor)

    def neighborhood(
        self,
        *,
        root_ref: str,
        depth: int,
        max_nodes: int,
    ) -> dict[str, Any]:
        root = self._entity(root_ref)
        queue: deque[tuple[Entity, int]] = deque([(root, 0)])
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        while queue and len(nodes) < max_nodes:
            entity, current_depth = queue.popleft()
            if entity.ref_id in nodes:
                continue
            nodes[entity.ref_id] = {
                "ref": entity.ref_id,
                "type": entity.entity_kind,
                "display_name": entity.display_name,
                "depth": current_depth,
            }
            if current_depth >= depth:
                continue
            neighbors: list[tuple[Entity, str, str]] = []
            for relation in self.session.scalars(
                select(Relationship).where(
                    Relationship.status == "active",
                    or_(
                        Relationship.subject_entity_id == entity.id,
                        Relationship.object_entity_id == entity.id,
                    ),
                )
            ):
                other_id = (
                    relation.object_entity_id
                    if relation.subject_entity_id == entity.id
                    else relation.subject_entity_id
                )
                other = self._entity_by_id(other_id)
                if other is not None:
                    neighbors.append(
                        (other, relation.relationship_type or "related", relation.ref_id)
                    )
            for affiliation in self.session.scalars(
                select(Affiliation).where(
                    Affiliation.status == "active",
                    or_(
                        Affiliation.subject_entity_id == entity.id,
                        Affiliation.organization_entity_id == entity.id,
                    ),
                )
            ):
                other_id = (
                    affiliation.organization_entity_id
                    if affiliation.subject_entity_id == entity.id
                    else affiliation.subject_entity_id
                )
                other = self._entity_by_id(other_id)
                if other is not None:
                    neighbors.append((other, "affiliated_with", affiliation.ref_id))
            profile = self.session.get(OrganizationInstitutionProfile, entity.id)
            if profile is not None and profile.parent_entity_id is not None:
                parent = self._entity_by_id(profile.parent_entity_id)
                if parent is not None:
                    neighbors.append((parent, "part_of", entity.ref_id))
            for child in self.session.scalars(
                select(OrganizationInstitutionProfile).where(
                    OrganizationInstitutionProfile.parent_entity_id == entity.id
                )
            ):
                child_entity = self._entity_by_id(child.entity_id)
                if child_entity is not None:
                    neighbors.append((child_entity, "contains", child_entity.ref_id))
            for other, predicate, edge_ref in neighbors:
                edge_from, edge_to = sorted((entity.ref_id, other.ref_id))
                edge_key = (edge_from, edge_to, predicate)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(
                        {
                            "ref": edge_ref,
                            "from": entity.ref_id,
                            "predicate": predicate,
                            "to": other.ref_id,
                        }
                    )
                if other.ref_id not in nodes:
                    queue.append((other, current_depth + 1))
        return self._bounded_context(
            {
                "ok": True,
                "root_ref": root.ref_id,
                "depth": depth,
                "nodes": list(nodes.values()),
                "edges": edges[: max_nodes * 3],
                "count": len(nodes),
                "truncated": bool(queue),
            }
        )

    def context_neighborhood(
        self,
        *,
        root_ref: str,
        depth: int,
        max_nodes: int,
    ) -> dict[str, Any]:
        """Traverse context primitives without conflating Items with Entities."""
        prefix, _payload = parse_public_ref(root_ref)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        queued: deque[tuple[str, int]] = deque([(root_ref, 0)])
        expanded: set[str] = set()

        def add_edge(source: str, predicate: str, target: str, edge_ref: str | None = None) -> None:
            edge = {"from": source, "predicate": predicate, "to": target}
            if edge_ref is not None:
                edge["ref"] = edge_ref
            if edge not in edges:
                edges.append(edge)

        def queue_ref(ref_id: str, current_depth: int) -> None:
            if ref_id not in expanded and current_depth <= depth:
                queued.append((ref_id, current_depth))

        if prefix == "ent":
            base = self.neighborhood(root_ref=root_ref, depth=depth, max_nodes=max_nodes)
            nodes.update({node["ref"]: node for node in base["nodes"]})
            edges.extend(base["edges"])
            for node in list(nodes.values()):
                node_ref = str(node["ref"])
                node_depth = int(node["depth"])
                if node_depth >= depth:
                    continue
                for item in self.session.scalars(
                    select(Item).where(Item.canonical_status == "active").limit(1000)
                ):
                    if node_ref in item.context_entity_refs:
                        nodes.setdefault(
                            item.ref_id,
                            {
                                "ref": item.ref_id,
                                "type": "item",
                                "display_name": item.title,
                                "depth": node_depth + 1,
                            },
                        )
                        add_edge(node_ref, "context_for", item.ref_id)
                        queue_ref(item.ref_id, node_depth + 1)
        elif prefix not in {"item", "task", "time", "evt"}:
            raise DocketError(
                code="unsupported_context_root",
                message="Context neighborhoods start from an Entity, Item, Task, Time, or Event.",
                details={"root_ref": root_ref},
            )

        while queued and len(nodes) < max_nodes:
            ref_id, current_depth = queued.popleft()
            if ref_id in expanded or current_depth > depth:
                continue
            expanded.add(ref_id)
            current_prefix, _ = parse_public_ref(ref_id)
            if current_prefix == "item":
                current_item = self.session.scalar(select(Item).where(Item.ref_id == ref_id))
                if current_item is None or current_item.canonical_status != "active":
                    raise DocketError(code="item_not_found", message="Item was not found.")
                nodes.setdefault(
                    ref_id,
                    {
                        "ref": ref_id,
                        "type": "item",
                        "display_name": current_item.title,
                        "depth": current_depth,
                    },
                )
                if current_depth >= depth:
                    continue
                for entity_ref in current_item.context_entity_refs:
                    entity = self.session.scalar(select(Entity).where(Entity.ref_id == entity_ref))
                    if entity is not None:
                        nodes.setdefault(
                            entity_ref,
                            {
                                "ref": entity_ref,
                                "type": entity.entity_kind,
                                "display_name": entity.display_name,
                                "depth": current_depth + 1,
                            },
                        )
                        add_edge(ref_id, "has_context", entity_ref)
                for task in self.session.scalars(
                    select(Task).where(Task.item_ref == ref_id, Task.canonical_status == "active")
                ):
                    add_edge(ref_id, "has_task", task.ref_id)
                    queue_ref(task.ref_id, current_depth + 1)
                for binding in self.session.scalars(
                    select(TemporalBinding).where(
                        TemporalBinding.subject_ref == ref_id,
                        TemporalBinding.canonical_status == "active",
                    )
                ):
                    add_edge(ref_id, binding.role, binding.ref_id)
                    queue_ref(binding.ref_id, current_depth + 1)
                for link in self.session.scalars(
                    select(EventItemLink).where(EventItemLink.item_ref == ref_id)
                ):
                    add_edge(ref_id, "realized_as_event", link.event_ref)
                    queue_ref(link.event_ref, current_depth + 1)
            elif current_prefix == "task":
                current_task = self.session.scalar(select(Task).where(Task.ref_id == ref_id))
                if current_task is None or current_task.canonical_status != "active":
                    raise DocketError(code="task_not_found", message="Task was not found.")
                nodes.setdefault(
                    ref_id,
                    {
                        "ref": ref_id,
                        "type": "task",
                        "display_name": current_task.title,
                        "state": current_task.task_state,
                        "depth": current_depth,
                    },
                )
                if current_depth < depth:
                    add_edge(ref_id, "about_item", current_task.item_ref)
                    queue_ref(current_task.item_ref, current_depth + 1)
                    for binding in self.session.scalars(
                        select(TemporalBinding).where(
                            TemporalBinding.subject_ref == ref_id,
                            TemporalBinding.canonical_status == "active",
                        )
                    ):
                        add_edge(ref_id, binding.role, binding.ref_id)
                        queue_ref(binding.ref_id, current_depth + 1)
            elif current_prefix == "time":
                current_binding = self.session.scalar(
                    select(TemporalBinding).where(TemporalBinding.ref_id == ref_id)
                )
                if current_binding is None or current_binding.canonical_status != "active":
                    raise DocketError(
                        code="temporal_binding_not_found", message="Time was not found."
                    )
                nodes.setdefault(
                    ref_id,
                    {
                        "ref": ref_id,
                        "type": "temporal_binding",
                        "display_name": current_binding.role,
                        "temporal_value": current_binding.temporal_value,
                        "depth": current_depth,
                    },
                )
                if current_depth < depth:
                    add_edge(ref_id, "describes", current_binding.subject_ref)
                    queue_ref(current_binding.subject_ref, current_depth + 1)
            elif current_prefix == "evt":
                event = self.session.scalar(
                    select(CanonicalEvent).where(CanonicalEvent.ref_id == ref_id)
                )
                if event is None or event.status in {"cancelled", "archived"}:
                    raise DocketError(code="event_not_found", message="Event was not found.")
                nodes.setdefault(
                    ref_id,
                    {
                        "ref": ref_id,
                        "type": "event",
                        "display_name": event.title,
                        "depth": current_depth,
                    },
                )
                if current_depth < depth:
                    for link in self.session.scalars(
                        select(EventItemLink).where(EventItemLink.event_ref == ref_id)
                    ):
                        add_edge(ref_id, "realizes_item", link.item_ref)
                        queue_ref(link.item_ref, current_depth + 1)

        ordered_nodes = sorted(nodes.values(), key=lambda node: (node["depth"], node["ref"]))
        return self._bounded_context(
            {
                "ok": True,
                "root_ref": root_ref,
                "depth": depth,
                "nodes": ordered_nodes[:max_nodes],
                "edges": edges[: max_nodes * 3],
                "count": min(len(ordered_nodes), max_nodes),
                "truncated": len(ordered_nodes) > max_nodes or bool(queued),
            }
        )

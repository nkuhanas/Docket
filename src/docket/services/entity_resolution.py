from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import AuditEvent, Entity, EntityAlias, IdentityHandle
from docket.schemas.registry import IdentityResolutionRequest
from docket.services.provenance_refs import ProvenanceRefService
from docket.services.registry import normalize_identity_value, normalize_registry_text


class DeterministicIdentityResolutionService:
    """Resolve identities only through the five frozen deterministic rules."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def observe_unbound_handle(
        self,
        *,
        handle_type: str,
        value: str,
        source_refs: list[str],
    ) -> IdentityHandle:
        ProvenanceRefService(self.session).require_all(source_refs)
        normalized = normalize_identity_value(handle_type, value)
        existing = self.session.scalar(
            select(IdentityHandle).where(
                IdentityHandle.handle_type == handle_type.casefold(),
                IdentityHandle.normalized_value == normalized,
            )
        )
        if existing is not None:
            return existing
        handle = IdentityHandle(
            handle_type=handle_type.casefold(),
            value=value,
            normalized_value=normalized,
            entity_id=None,
            binding_rule=None,
            binding_basis_refs=[],
            status="unbound",
            basis_refs=list(source_refs),
            decision_refs=[],
            source_refs=list(source_refs),
            created_by_changeset_ref=None,
        )
        self.session.add(handle)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="identity_handle.observed",
                entity_type="identity_handle",
                entity_id=handle.id,
                actor_type="docket_observer",
                actor_id=None,
                request_id=None,
                primary_ref=handle.ref_id,
                affected_refs=[handle.ref_id],
                basis_refs=list(source_refs),
                data={"handle_type": handle.handle_type, "binding_state": "unbound"},
            )
        )
        return handle

    @staticmethod
    def _projection(
        *,
        state: str,
        entity: Entity | None,
        rule: str | None,
        basis_refs: list[str],
        candidates: list[Entity] | None = None,
        suggestions: list[Entity] | None = None,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "entity_ref": entity.ref_id if entity is not None else None,
            "resolution_rule": rule,
            "basis_refs": basis_refs,
            "candidates": [
                {"ref": candidate.ref_id, "display_name": candidate.canonical_name}
                for candidate in candidates or []
            ],
            "suggestions": [
                {"ref": candidate.ref_id, "display_name": candidate.canonical_name}
                for candidate in suggestions or []
            ],
        }

    def _registered(self, ref_id: str) -> Entity:
        entity = self.session.scalar(
            select(Entity).where(
                Entity.ref_id == ref_id,
                Entity.registration_state == "registered",
                Entity.status == "active",
            )
        )
        if entity is None:
            raise DocketError(
                code="registered_entity_not_found",
                message="Deterministic resolution target is not a registered active Entity.",
                details={"entity_ref": ref_id},
            )
        return entity

    def _record_resolution(
        self,
        entity: Entity,
        *,
        rule: str,
        basis_refs: list[str],
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type="identity.resolved",
                entity_type="entity",
                entity_id=entity.id,
                actor_type="docket_resolver",
                actor_id=None,
                request_id=None,
                primary_ref=entity.ref_id,
                affected_refs=[entity.ref_id],
                basis_refs=list(basis_refs),
                data={"resolution_rule": rule},
            )
        )

    def _suggestions(self, request: IdentityResolutionRequest) -> list[Entity]:
        query = normalize_registry_text(request.mention or request.handle_value or "")
        if not query:
            return []
        statement = select(Entity).where(
            Entity.registration_state == "registered",
            Entity.status == "active",
        )
        if request.entity_kind is not None:
            statement = statement.where(Entity.entity_class == request.entity_kind)
        ranked = sorted(
            self.session.scalars(statement),
            key=lambda item: SequenceMatcher(
                None, query, item.normalized_name
            ).ratio(),
            reverse=True,
        )
        return [
            item
            for item in ranked[:5]
            if SequenceMatcher(None, query, item.normalized_name).ratio() >= 0.35
        ]

    def resolve(self, request: IdentityResolutionRequest) -> dict[str, Any]:
        ProvenanceRefService(self.session).require_all(request.basis_refs)

        # Rule 1 and rule 3: an exact active IdentityHandle binding.
        if request.handle_type is not None and request.handle_value is not None:
            normalized = normalize_identity_value(
                request.handle_type, request.handle_value
            )
            handles = list(
                self.session.scalars(
                    select(IdentityHandle).where(
                        IdentityHandle.handle_type == request.handle_type.casefold(),
                        IdentityHandle.normalized_value == normalized,
                        IdentityHandle.status == "bound",
                    )
                )
            )
            entities = {
                handle.entity_id: self.session.get(Entity, handle.entity_id)
                for handle in handles
                if handle.entity_id is not None
            }
            candidates = [
                entity
                for entity in entities.values()
                if entity is not None
                and entity.registration_state == "registered"
                and entity.status == "active"
            ]
            if len(candidates) > 1:
                return self._projection(
                    state="ambiguous",
                    entity=None,
                    rule="exact_identity_handle",
                    basis_refs=request.basis_refs,
                    candidates=candidates,
                )
            if len(candidates) == 1:
                entity = candidates[0]
                handle = handles[0]
                rule = (
                    "provider_authoritative"
                    if handle.binding_rule == "provider_authoritative"
                    else "exact_identity_handle"
                )
                basis_refs = [*request.basis_refs, handle.ref_id]
                self._record_resolution(entity, rule=rule, basis_refs=basis_refs)
                return self._projection(
                    state="resolved",
                    entity=entity,
                    rule=rule,
                    basis_refs=basis_refs,
                )

        # Rule 2: exact Operator-authored alias.
        if request.mention is not None:
            normalized_mention = normalize_registry_text(request.mention)
            aliases = list(
                self.session.scalars(
                    select(EntityAlias).where(
                        EntityAlias.normalized_alias == normalized_mention,
                        EntityAlias.authority.in_(("explicit_user", "operator_utterance")),
                    )
                )
            )
            candidates = []
            for alias in aliases:
                alias_entity = self.session.get(Entity, alias.entity_id)
                if (
                    alias_entity is not None
                    and alias_entity.registration_state == "registered"
                    and alias_entity.status == "active"
                ):
                    candidates.append(alias_entity)
            candidates = list({candidate.id: candidate for candidate in candidates}.values())
            if len(candidates) > 1:
                return self._projection(
                    state="ambiguous",
                    entity=None,
                    rule="operator_alias",
                    basis_refs=request.basis_refs,
                    candidates=candidates,
                )
            if len(candidates) == 1:
                entity = candidates[0]
                self._record_resolution(
                    entity, rule="operator_alias", basis_refs=request.basis_refs
                )
                return self._projection(
                    state="resolved",
                    entity=entity,
                    rule="operator_alias",
                    basis_refs=request.basis_refs,
                )

        # Rule 4: explicit Entity ref in the current authenticated utterance.
        if request.explicit_entity_ref is not None:
            entity = self._registered(request.explicit_entity_ref)
            self._record_resolution(
                entity, rule="explicit_entity_ref", basis_refs=request.basis_refs
            )
            return self._projection(
                state="resolved",
                entity=entity,
                rule="explicit_entity_ref",
                basis_refs=request.basis_refs,
            )

        # Rule 5: explicit Operator correction/selection in the IntentSession.
        if request.operator_selected_ref is not None:
            entity = self._registered(request.operator_selected_ref)
            self._record_resolution(
                entity, rule="operator_selection", basis_refs=request.basis_refs
            )
            return self._projection(
                state="resolved",
                entity=entity,
                rule="operator_selection",
                basis_refs=request.basis_refs,
            )

        return self._projection(
            state="unresolved",
            entity=None,
            rule=None,
            basis_refs=request.basis_refs,
            suggestions=self._suggestions(request),
        )

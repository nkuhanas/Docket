from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    Affiliation,
    AuditEvent,
    ChangeSet,
    Entity,
    Fact,
    IdentityBinding,
    IdentityHandle,
    Interaction,
    InteractionParticipant,
    Item,
    OrganizationInstitutionProfile,
    PersonProfile,
    Relationship,
    SenderIdentityEmail,
)
from docket.models.base import utc_now
from docket.schemas.registry import (
    AffiliationCreateSpec,
    EntityCreateSpec,
    EntityPatchSpec,
    FactCreateSpec,
    IdentityHandleOnlyCreateSpec,
    InteractionCreateSpec,
    RelationshipCreateSpec,
)

RegistryHandler = Callable[[Session, ChangeSet, Any], list[str]]
_SPACE = re.compile(r"\s+")


def normalize_registry_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return _SPACE.sub(" ", normalized)


def normalize_identity_value(handle_type: str, value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if handle_type.casefold() in {"email", "google_attendee", "calendly"}:
        return normalized.casefold()
    if handle_type.casefold() == "phone":
        return "".join(character for character in normalized if character.isdecimal())
    return normalize_registry_text(normalized)


class RegistryService:
    """Apply typed registry effects inside the owning ChangeSet transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, RegistryHandler]:
        return {
            "entity": self.apply_entity,
            "identity_handle": self.apply_identity_handle,
            "identity_binding": self.apply_identity_handle,
            "affiliation": self.apply_affiliation,
            "relationship": self.apply_relationship,
            "fact": self.apply_fact,
            "interaction": self.apply_interaction,
        }

    @staticmethod
    def _provenance(
        changeset: ChangeSet,
        change: Any,
        *,
        extra_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        decision_refs = [
            ref_id for ref_id in change.basis_refs if parse_public_ref(ref_id)[0] == "dec"
        ]
        source_refs = [
            ref_id for ref_id in change.basis_refs if parse_public_ref(ref_id)[0] == "src"
        ]
        for ref_id in extra_sources or []:
            if ref_id not in source_refs:
                source_refs.append(ref_id)
        return {
            "basis_refs": list(change.basis_refs),
            "decision_refs": decision_refs,
            "source_refs": source_refs,
            "created_by_changeset_ref": changeset.ref_id,
        }

    def _entity(self, ref_id: str, *, active: bool = True) -> Entity:
        entity = self.session.scalar(select(Entity).where(Entity.ref_id == ref_id))
        if entity is None or (active and entity.canonical_status != "active"):
            raise DocketError(
                code="active_entity_not_found",
                message="A required active Entity public reference was not found.",
                details={"entity_ref": ref_id},
            )
        return entity

    def _identity_handle(self, ref_id: str) -> IdentityHandle:
        handle = self.session.scalar(select(IdentityHandle).where(IdentityHandle.ref_id == ref_id))
        if handle is None:
            raise DocketError(
                code="identity_handle_not_found",
                message="IdentityHandle was not found.",
                details={"identity_ref": ref_id},
            )
        return handle

    def _add_sender_email(
        self,
        *,
        sender: IdentityHandle,
        email_ref: str,
        changeset: ChangeSet,
        change: Any,
    ) -> str:
        if sender.handle_type != "sender_label":
            raise DocketError(
                code="sender_identity_required",
                message="Associated emails require a sender-label IdentityHandle.",
                details={"identity_ref": sender.ref_id},
            )
        email = self._identity_handle(email_ref)
        if email.handle_type != "email" or email.status not in {"unbound", "bound"}:
            raise DocketError(
                code="active_email_identity_required",
                message="A sender association requires an active exact email IdentityHandle.",
                details={
                    "identity_ref": email.ref_id,
                    "handle_type": email.handle_type,
                    "status": email.status,
                },
            )
        active = self.session.scalar(
            select(SenderIdentityEmail).where(
                SenderIdentityEmail.email_identity_handle_id == email.id,
                SenderIdentityEmail.status == "active",
            )
        )
        if active is not None:
            if active.sender_identity_handle_id == sender.id:
                return email.ref_id
            conflicting_sender_ref = self.session.scalar(
                select(IdentityHandle.ref_id).where(
                    IdentityHandle.id == active.sender_identity_handle_id
                )
            )
            raise DocketError(
                code="sender_email_association_conflict",
                message="The exact email is already associated with another sender handle.",
                details={
                    "email_identity_ref": email.ref_id,
                    "sender_identity_ref": conflicting_sender_ref,
                },
            )
        association = self.session.scalar(
            select(SenderIdentityEmail).where(
                SenderIdentityEmail.sender_identity_handle_id == sender.id,
                SenderIdentityEmail.email_identity_handle_id == email.id,
            )
        )
        source_refs = list(
            dict.fromkeys(
                [
                    *self._provenance(changeset, change)["source_refs"],
                    *email.source_refs,
                ]
            )
        )
        if association is None:
            association = SenderIdentityEmail(
                sender_identity_handle_id=sender.id,
                email_identity_handle_id=email.id,
                status="active",
                basis_refs=list(change.basis_refs),
                source_refs=source_refs,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(association)
        else:
            association.status = "active"
            association.valid_from = utc_now()
            association.valid_to = None
            association.basis_refs = list(change.basis_refs)
            association.source_refs = source_refs
            association.created_by_changeset_ref = changeset.ref_id
        return email.ref_id

    def _remove_sender_email(
        self,
        *,
        sender: IdentityHandle,
        email_ref: str,
        retract: bool,
    ) -> str:
        email = self._identity_handle(email_ref)
        association = self.session.scalar(
            select(SenderIdentityEmail).where(
                SenderIdentityEmail.sender_identity_handle_id == sender.id,
                SenderIdentityEmail.email_identity_handle_id == email.id,
                SenderIdentityEmail.status == "active",
            )
        )
        if association is None:
            raise DocketError(
                code="sender_email_association_not_found",
                message="The sender handle has no active association to that email.",
                details={
                    "sender_identity_ref": sender.ref_id,
                    "email_identity_ref": email.ref_id,
                },
            )
        association.status = "retracted" if retract else "historical"
        association.valid_to = utc_now()
        return email.ref_id

    def _audit(
        self,
        *,
        event_type: str,
        item: Any,
        changeset: ChangeSet,
        change: Any,
        affected_refs: list[str],
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type=change.object_type,
                entity_id=item.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                request_id=None,
                primary_ref=item.ref_id,
                affected_refs=affected_refs,
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "change_id": change.change_id,
                    "action": change.action,
                },
            )
        )

    def _upsert_profile(self, entity: Entity, spec: EntityCreateSpec) -> None:
        if spec.entity_kind == "person":
            person_profile = self.session.get(PersonProfile, entity.id)
            if person_profile is None:
                person_profile = PersonProfile(entity_id=entity.id)
                self.session.add(person_profile)
            person_profile.preferred_name = spec.preferred_name
            person_profile.pronouns = spec.pronouns
            person_profile.is_operator = spec.is_operator
        elif spec.entity_kind in {"organization", "institution"}:
            parent = (
                self._entity(spec.parent_entity_ref) if spec.parent_entity_ref is not None else None
            )
            if parent is not None and parent.entity_kind not in {
                "organization",
                "institution",
            }:
                raise DocketError(
                    code="invalid_institutional_parent",
                    message=(
                        "Institutional hierarchy parents must be Organizations or Institutions."
                    ),
                )
            organization_profile = self.session.get(OrganizationInstitutionProfile, entity.id)
            if organization_profile is None:
                organization_profile = OrganizationInstitutionProfile(
                    entity_id=entity.id,
                    entity_kind=spec.entity_kind,
                )
                self.session.add(organization_profile)
            organization_profile.entity_kind = spec.entity_kind
            organization_profile.parent_entity_id = parent.id if parent is not None else None
            organization_profile.organization_type = spec.organization_type
            organization_profile.description = spec.description

    def apply_entity(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        if change.action == "create":
            spec = EntityCreateSpec.model_validate(change.create_spec)
            normalized_name = normalize_registry_text(spec.display_name)
            candidates = list(
                self.session.scalars(
                    select(Entity).where(
                        Entity.entity_kind == spec.entity_kind,
                        Entity.normalized_name == normalized_name,
                        Entity.canonical_status == "active",
                    )
                )
            )
            if len(candidates) > 1:
                raise DocketError(
                    code="entity_resolution_ambiguous",
                    message="More than one Entity matches this exact registration identity.",
                    details={"candidate_refs": [item.ref_id for item in candidates]},
                )
            if candidates:
                entity = candidates[0]
                if spec.ref_id is not None and entity.ref_id != spec.ref_id:
                    raise DocketError(
                        code="planned_ref_identity_conflict",
                        message=(
                            "The planned Entity reference conflicts with an existing "
                            "exact canonical identity."
                        ),
                        details={"existing_ref": entity.ref_id, "planned_ref": spec.ref_id},
                    )
                self._upsert_profile(entity, spec)
                return [entity.ref_id]
            else:
                provenance = self._provenance(changeset, change)
                entity_values: dict[str, Any] = {}
                if spec.ref_id is not None:
                    entity_values["ref_id"] = spec.ref_id
                entity = Entity(
                    entity_kind=spec.entity_kind,
                    display_name=spec.display_name.strip(),
                    normalized_name=normalized_name,
                    canonical_status="active",
                    attributes_json={},
                    **provenance,
                    **entity_values,
                )
                self.session.add(entity)
                self.session.flush()
            self._upsert_profile(entity, spec)
            self._audit(
                event_type="entity.registered",
                item=entity,
                changeset=changeset,
                change=change,
                affected_refs=[entity.ref_id],
            )
            return [entity.ref_id]

        if change.object_ref is None:
            raise DocketError(code="entity_ref_required", message="Entity update requires a ref.")
        entity = self._entity(change.object_ref)
        if change.action == "retract":
            entity.canonical_status = "retracted"
        elif change.action in {"update", "supersede"}:
            patch = EntityPatchSpec.model_validate(change.payload)
            display_name = patch.display_name
            if display_name is not None:
                entity.display_name = display_name.strip()
                entity.normalized_name = normalize_registry_text(str(display_name))
            if patch.canonical_status is not None:
                entity.canonical_status = patch.canonical_status
        else:
            raise DocketError(code="unsupported_entity_action", message="Unsupported action.")
        entity.version += 1
        self._audit(
            event_type="entity.changed",
            item=entity,
            changeset=changeset,
            change=change,
            affected_refs=[entity.ref_id],
        )
        return [entity.ref_id]

    def apply_identity_handle(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        handle: IdentityHandle
        if change.action == "create":
            spec = IdentityHandleOnlyCreateSpec.model_validate(change.create_spec)
            normalized = normalize_identity_value(spec.handle_type, spec.value)
            existing = self.session.scalar(
                select(IdentityHandle).where(
                    IdentityHandle.handle_type == spec.handle_type.casefold(),
                    IdentityHandle.normalized_value == normalized,
                )
            )
            if existing is not None:
                if spec.ref_id is not None and spec.ref_id != existing.ref_id:
                    raise DocketError(
                        code="planned_ref_identity_conflict",
                        message="The planned IdentityHandle ref conflicts with an exact handle.",
                        details={
                            "existing_ref": existing.ref_id,
                            "planned_ref": spec.ref_id,
                        },
                    )
                if spec.associated_email_refs:
                    active_email_ids = set(
                        self.session.scalars(
                            select(SenderIdentityEmail.email_identity_handle_id).where(
                                SenderIdentityEmail.sender_identity_handle_id == existing.id,
                                SenderIdentityEmail.status == "active",
                            )
                        )
                    )
                    requested_email_ids = {
                        self._identity_handle(ref_id).id for ref_id in spec.associated_email_refs
                    }
                    if not requested_email_ids.issubset(active_email_ids):
                        raise DocketError(
                            code="identity_handle_exists_requires_update",
                            message=(
                                "Add new sender email associations by updating the exact "
                                "existing sender handle."
                            ),
                            details={"identity_ref": existing.ref_id},
                        )
                return [existing.ref_id]
            provenance = self._provenance(changeset, change, extra_sources=list(spec.source_refs))
            handle_values: dict[str, Any] = {}
            if spec.ref_id is not None:
                handle_values["ref_id"] = spec.ref_id
            handle = IdentityHandle(
                handle_type=spec.handle_type.casefold(),
                value=spec.value,
                normalized_value=normalized,
                entity_id=None,
                binding_rule=None,
                binding_basis_refs=[],
                status="unbound",
                **provenance,
                **handle_values,
            )
            self.session.add(handle)
            self.session.flush()
            affected_refs = [handle.ref_id]
            for email_ref in spec.associated_email_refs:
                affected_refs.append(
                    self._add_sender_email(
                        sender=handle,
                        email_ref=email_ref,
                        changeset=changeset,
                        change=change,
                    )
                )
        else:
            if change.object_ref is None:
                raise DocketError(
                    code="identity_ref_required", message="Identity mutation requires a ref."
                )
            handle = self._identity_handle(change.object_ref)
            affected_refs = [handle.ref_id]
            if change.action in {"update", "supersede"}:
                if handle.handle_type != "sender_label":
                    raise DocketError(
                        code="unsupported_identity_action",
                        message="Only sender-label handles support association updates.",
                    )
                allowed = {"add_associated_email_ref", "remove_associated_email_ref"}
                unknown = set(change.payload) - allowed
                if unknown or len(change.payload) != 1:
                    raise DocketError(
                        code="invalid_sender_identity_patch",
                        message=(
                            "Sender identity updates add or remove exactly one email association."
                        ),
                        details={"fields": sorted(set(change.payload))},
                    )
                add_ref = change.payload.get("add_associated_email_ref")
                remove_ref = change.payload.get("remove_associated_email_ref")
                if isinstance(add_ref, str):
                    affected_refs.append(
                        self._add_sender_email(
                            sender=handle,
                            email_ref=add_ref,
                            changeset=changeset,
                            change=change,
                        )
                    )
                elif isinstance(remove_ref, str):
                    affected_refs.append(
                        self._remove_sender_email(
                            sender=handle,
                            email_ref=remove_ref,
                            retract=change.action == "supersede",
                        )
                    )
                else:
                    raise DocketError(
                        code="invalid_sender_identity_patch",
                        message="Sender email association requires an exact IdentityHandle ref.",
                    )
            if change.action == "bind":
                if handle.handle_type == "sender_label":
                    raise DocketError(
                        code="sender_identity_entity_binding_forbidden",
                        message="A sender-label index cannot bind directly to an Entity.",
                    )
                entity_ref = str(change.payload.get("entity_ref", ""))
                entity = self._entity(entity_ref)
                resolution_basis = change.payload.get("resolution_basis")
                binding_rule = (
                    str(resolution_basis.get("kind", ""))
                    if isinstance(resolution_basis, dict)
                    else ""
                )
                if binding_rule not in {
                    "exact_identity_handle",
                    "operator_alias",
                    "provider_authoritative",
                    "explicit_entity_ref",
                    "operator_selection",
                }:
                    raise DocketError(
                        code="invalid_identity_resolution_basis",
                        message="Identity binding requires a deterministic resolution rule.",
                    )
                if binding_rule == "operator_selection":
                    selection_ref = str(resolution_basis.get("utterance_ref", ""))
                    if selection_ref not in change.basis_refs:
                        raise DocketError(
                            code="invalid_identity_resolution_basis",
                            message=(
                                "Operator-selection identity binding must cite its exact "
                                "selection utterance in basis_refs."
                            ),
                        )
                active_bindings = list(
                    self.session.scalars(
                        select(IdentityBinding).where(
                            IdentityBinding.identity_handle_id == handle.id,
                            IdentityBinding.status == "active",
                        )
                    )
                )
                if len(active_bindings) > 1:
                    raise DocketError(
                        code="identity_binding_state_corrupt",
                        message="IdentityHandle has more than one active binding.",
                    )
                if not active_bindings or active_bindings[0].entity_id != entity.id:
                    for active in active_bindings:
                        active.status = "historical"
                        active.valid_to = utc_now()
                    self.session.add(
                        IdentityBinding(
                            identity_handle_id=handle.id,
                            entity_id=entity.id,
                            binding_rule=binding_rule,
                            status="active",
                            **self._provenance(changeset, change),
                        )
                    )
                handle.entity_id = entity.id
                handle.binding_rule = binding_rule
                handle.binding_basis_refs = list(change.basis_refs)
                handle.status = "bound"
            elif change.action in {"unbind", "retract"}:
                for active in self.session.scalars(
                    select(IdentityBinding).where(
                        IdentityBinding.identity_handle_id == handle.id,
                        IdentityBinding.status == "active",
                    )
                ):
                    active.status = "retracted" if change.action == "retract" else "historical"
                    active.valid_to = utc_now()
                handle.status = "retracted" if change.action == "retract" else "unbound"
                handle.entity_id = None
                handle.binding_rule = None
                handle.binding_basis_refs = []
                if handle.handle_type == "sender_label":
                    for association in self.session.scalars(
                        select(SenderIdentityEmail).where(
                            SenderIdentityEmail.sender_identity_handle_id == handle.id,
                            SenderIdentityEmail.status == "active",
                        )
                    ):
                        association.status = (
                            "retracted" if change.action == "retract" else "historical"
                        )
                        association.valid_to = utc_now()
            elif change.action not in {"update", "supersede"}:
                raise DocketError(code="unsupported_identity_action", message="Unsupported action.")
            handle.version += 1
        self._audit(
            event_type="identity_handle.changed",
            item=handle,
            changeset=changeset,
            change=change,
            affected_refs=affected_refs,
        )
        return [handle.ref_id]

    def _apply_assertion(
        self,
        *,
        changeset: ChangeSet,
        change: Any,
        model: type[Any],
        schema: type[Any],
        creator: Callable[[Any, dict[str, Any]], Any],
    ) -> list[str]:
        if change.action == "create":
            spec = schema.model_validate(change.create_spec)
            item = creator(spec, self._provenance(changeset, change))
            self.session.add(item)
            self.session.flush()
            refs = [item.ref_id]
        else:
            if change.object_ref is None:
                raise DocketError(
                    code="assertion_ref_required", message="Assertion mutation requires a ref."
                )
            item = self.session.scalar(select(model).where(model.ref_id == change.object_ref))
            if item is None:
                raise DocketError(
                    code="canonical_assertion_not_found",
                    message="Canonical assertion public reference was not found.",
                )
            refs = [item.ref_id]
            if change.action == "supersede":
                replacement = change.payload.get("replacement")
                spec = schema.model_validate(replacement)
                item.status = "historical"
                item.version += 1
                replacement_item = creator(spec, self._provenance(changeset, change))
                self.session.add(replacement_item)
                self.session.flush()
                refs.append(replacement_item.ref_id)
            elif change.action == "retract":
                item.status = "retracted"
                item.version += 1
            elif change.action == "update":
                if set(change.payload) - {"valid_to", "status"}:
                    raise DocketError(
                        code="invalid_assertion_patch",
                        message="Assertion update only supports valid_to and status.",
                    )
                if "valid_to" in change.payload:
                    item.valid_to = change.payload["valid_to"]
                if "status" in change.payload:
                    if change.payload["status"] not in {"active", "historical"}:
                        raise DocketError(
                            code="invalid_assertion_status",
                            message="Assertion status must be active or historical.",
                        )
                    item.status = change.payload["status"]
                item.version += 1
            else:
                raise DocketError(
                    code="unsupported_assertion_action", message="Unsupported action."
                )
        self._audit(
            event_type=f"{change.object_type}.changed",
            item=item,
            changeset=changeset,
            change=change,
            affected_refs=refs,
        )
        return refs

    def apply_affiliation(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        def create(spec: AffiliationCreateSpec, provenance: dict[str, Any]) -> Affiliation:
            if spec.subject_ref is None or spec.organization_ref is None:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="Affiliation dependencies were not resolved before execution.",
                )
            subject = self._entity(spec.subject_ref)
            organization = self._entity(spec.organization_ref)
            if organization.entity_kind not in {"organization", "institution"}:
                raise DocketError(
                    code="invalid_affiliation_target",
                    message="Affiliation target must be an Organization or Institution.",
                )
            return Affiliation(
                subject_entity_id=subject.id,
                organization_entity_id=organization.id,
                role=spec.role,
                domain=spec.domain,
                valid_from=spec.valid_from,
                valid_to=spec.valid_to,
                status=spec.status,
                **provenance,
            )

        return self._apply_assertion(
            changeset=changeset,
            change=change,
            model=Affiliation,
            schema=AffiliationCreateSpec,
            creator=create,
        )

    def apply_relationship(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        def create(spec: RelationshipCreateSpec, provenance: dict[str, Any]) -> Relationship:
            if spec.subject_ref is None or spec.object_ref is None:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="Relationship dependencies were not resolved before execution.",
                )
            return Relationship(
                subject_entity_id=self._entity(spec.subject_ref).id,
                object_entity_id=self._entity(spec.object_ref).id,
                relationship_type=spec.relationship_type,
                context=spec.context,
                valid_from=spec.valid_from,
                valid_to=spec.valid_to,
                status=spec.status,
                **provenance,
            )

        return self._apply_assertion(
            changeset=changeset,
            change=change,
            model=Relationship,
            schema=RelationshipCreateSpec,
            creator=create,
        )

    def apply_fact(
        self, _session: Session, changeset: ChangeSet, change: Any
    ) -> list[str]:
        def create(spec: FactCreateSpec, provenance: dict[str, Any]) -> Fact:
            if spec.subject_ref is None:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="Fact dependency was not resolved before execution.",
                )
            subject_prefix, _payload = parse_public_ref(spec.subject_ref)
            subject = (
                self._entity(spec.subject_ref)
                if subject_prefix == "ent"
                else self.session.scalar(select(Item).where(Item.ref_id == spec.subject_ref))
            )
            if subject is None:
                raise DocketError(
                    code="fact_subject_not_found",
                    message="Fact subject public reference was not found.",
                    details={"subject_ref": spec.subject_ref},
                )
            return Fact(
                subject_ref=spec.subject_ref,
                predicate=spec.predicate,
                value_json=spec.value_json,
                valid_from=spec.valid_from,
                valid_to=spec.valid_to,
                status=spec.status,
                **provenance,
            )

        return self._apply_assertion(
            changeset=changeset,
            change=change,
            model=Fact,
            schema=FactCreateSpec,
            creator=create,
        )

    def apply_interaction(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        if change.action != "create":
            raise DocketError(
                code="unsupported_interaction_action",
                message="Interactions are immutable; use a new historical transition.",
            )
        spec = InteractionCreateSpec.model_validate(change.create_spec)
        place = self._entity(spec.place_ref) if spec.place_ref is not None else None
        for organization_ref in spec.organization_refs:
            organization = self._entity(organization_ref)
            if organization.entity_kind not in {"organization", "institution"}:
                raise DocketError(
                    code="invalid_interaction_organization",
                    message="Interaction organization refs must identify institutions.",
                )
        interaction = Interaction(
            interaction_type=spec.interaction_type,
            occurred_at=spec.occurred_at,
            ended_at=spec.ended_at,
            summary=spec.summary,
            event_ref=spec.event_ref,
            place_entity_id=place.id if place is not None else None,
            organization_refs=list(spec.organization_refs),
            status="active",
            **self._provenance(changeset, change, extra_sources=list(spec.source_refs)),
        )
        self.session.add(interaction)
        self.session.flush()
        participant_refs: list[str] = []
        for participant in spec.participants:
            if participant.entity_ref is None:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="Interaction participant was not resolved before execution.",
                )
            entity = self._entity(participant.entity_ref)
            participant_refs.append(participant.entity_ref)
            self.session.add(
                InteractionParticipant(
                    interaction_id=interaction.id,
                    entity_id=entity.id,
                    role=participant.role,
                )
            )
        self._audit(
            event_type="interaction.created",
            item=interaction,
            changeset=changeset,
            change=change,
            affected_refs=[
                interaction.ref_id,
                *participant_refs,
            ],
        )
        return [interaction.ref_id]

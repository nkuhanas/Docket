from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import is_public_ref, parse_public_ref
from docket.models import (
    Affiliation,
    AuditEvent,
    ChangeSet,
    ChangeSetRevision,
    Conflict,
    Entity,
    Fact,
    IntentSession,
    IntentTurn,
    LaneRoutingDecision,
    OperatorUtterance,
    Relationship,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    CanonicalChangeInput,
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ChangeSetRevise,
    ConflictResolve,
    ProviderIntentInput,
)
from docket.services.conflicts import ConflictService
from docket.services.intent_sessions import IntentSessionService
from docket.services.provenance_refs import ProvenanceRefService

CanonicalChangeHandler = Callable[
    [Session, ChangeSet, CanonicalChangeInput],
    list[str],
]
ProviderIntentHandler = Callable[
    [Session, ChangeSet, ProviderIntentInput, dict[str, list[str]]],
    list[str],
]

_GROUP_TYPES: dict[str, frozenset[str]] = {
    "registry_changes": frozenset(
        {
            "entity",
            "identity_binding",
            "affiliation",
            "relationship",
            "fact",
            "interaction",
        }
    ),
    "preference_changes": frozenset({"preference"}),
    "lane_changes": frozenset({"calendar_lane", "lane_routing_decision"}),
    "event_changes": frozenset({"canonical_event"}),
    "resolution_changes": frozenset({"conflict_resolution", "attention_case_resolution"}),
}

_OBJECT_PREFIXES: dict[str, str] = {
    "entity": "ent",
    "identity_binding": "idn",
    "affiliation": "aff",
    "relationship": "rel",
    "fact": "fact",
    "interaction": "int",
    "preference": "pref",
    "calendar_lane": "lane",
    "lane_routing_decision": "route",
    "canonical_event": "evt",
    "conflict_resolution": "cnf",
    "attention_case_resolution": "case",
}


def _content_payload(content: ChangeSetContent) -> dict[str, Any]:
    return content.model_dump(mode="json")


def _all_changes(content: ChangeSetContent) -> list[CanonicalChangeInput]:
    pending = [change for group_name in _GROUP_TYPES for change in getattr(content, group_name)]
    ordered: list[CanonicalChangeInput] = []
    resolved_ids: set[str] = set()
    while pending:
        ready = next(
            (
                change
                for change in pending
                if _nested_change_refs(change.create_spec or change.payload) <= resolved_ids
            ),
            None,
        )
        if ready is None:
            # Validation reports unknown/cyclic create references. Preserve stable
            # input order here so preparation can return a durable clarification.
            ordered.extend(pending)
            break
        pending.remove(ready)
        ordered.append(ready)
        resolved_ids.add(ready.change_id)
    return ordered


def _nested_public_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str) and is_public_ref(value):
        refs.add(value)
    elif isinstance(value, dict):
        for nested in value.values():
            refs.update(_nested_public_refs(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            refs.update(_nested_public_refs(nested))
    return refs


def _nested_change_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.endswith("_change_id") and isinstance(nested, str):
                refs.add(nested)
            elif key.endswith("_change_ids") and isinstance(nested, list):
                refs.update(str(item) for item in nested)
            else:
                refs.update(_nested_change_refs(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            refs.update(_nested_change_refs(nested))
    return refs


def _resolved_create_spec(
    value: Any,
    refs_by_change_id: dict[str, list[str]],
) -> Any:
    """Resolve explicit in-ChangeSet create references after their targets commit."""

    if isinstance(value, list):
        return [_resolved_create_spec(item, refs_by_change_id) for item in value]
    if not isinstance(value, dict):
        return value
    resolved: dict[str, Any] = {}
    for key, nested in value.items():
        if key.endswith("_change_id"):
            target_key = f"{key[:-10]}_ref"
            refs = refs_by_change_id.get(str(nested))
            if refs is None or len(refs) != 1:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="A ChangeSet-local create reference did not resolve exactly once.",
                    details={"change_id": nested, "field": key},
                )
            if target_key in value:
                raise DocketError(
                    code="duplicate_create_reference",
                    message="Use either a public ref or a create change id, not both.",
                    details={"field": target_key},
                )
            resolved[target_key] = refs[0]
        elif key.endswith("_change_ids"):
            target_key = f"{key[:-11]}_refs"
            if target_key in value:
                raise DocketError(
                    code="duplicate_create_reference",
                    message="Use either public refs or create change ids, not both.",
                    details={"field": target_key},
                )
            target_refs: list[str] = []
            for change_id in nested:
                refs = refs_by_change_id.get(str(change_id))
                if refs is None or len(refs) != 1:
                    raise DocketError(
                        code="create_reference_unresolved",
                        message=(
                            "A ChangeSet-local create reference did not resolve exactly once."
                        ),
                        details={"change_id": change_id, "field": key},
                    )
                target_refs.append(refs[0])
            resolved[target_key] = target_refs
        else:
            resolved[key] = _resolved_create_spec(nested, refs_by_change_id)
    return resolved


class ChangeSetService:
    """Compile and atomically commit Operator-authorized canonical transitions."""

    def __init__(
        self,
        session: Session,
        *,
        handlers: dict[str, CanonicalChangeHandler] | None = None,
        provider_handler: ProviderIntentHandler | None = None,
    ) -> None:
        self.session = session
        self.handlers: dict[str, CanonicalChangeHandler] = {
            "conflict_resolution": self._apply_conflict_resolution,
            **(handlers or {}),
        }
        self.provider_handler = provider_handler

    @staticmethod
    def _apply_conflict_resolution(
        session: Session,
        _changeset: ChangeSet,
        change: CanonicalChangeInput,
    ) -> list[str]:
        if change.object_ref is None:
            raise DocketError(
                code="conflict_ref_required",
                message="Conflict resolution change requires a Conflict reference.",
            )
        request = ConflictResolve.model_validate(
            {
                "conflict_ref": change.object_ref,
                **change.payload,
            }
        )
        conflict, decision = ConflictService(session).resolve(request)
        return [conflict.ref_id, decision.ref_id]

    def get(self, changeset_ref: str) -> ChangeSet:
        changeset = self.session.scalar(select(ChangeSet).where(ChangeSet.ref_id == changeset_ref))
        if changeset is None:
            raise DocketError(
                code="changeset_not_found",
                message="ChangeSet public reference was not found.",
                details={"changeset_ref": changeset_ref},
            )
        return changeset

    @staticmethod
    def _content(changeset: ChangeSet) -> ChangeSetContent:
        return ChangeSetContent.model_validate(
            {
                "basis_refs": changeset.basis_refs,
                "expected_versions": changeset.expected_versions,
                "registry_changes": changeset.registry_changes,
                "preference_changes": changeset.preference_changes,
                "lane_changes": changeset.lane_changes,
                "event_changes": changeset.event_changes,
                "resolution_changes": changeset.resolution_changes,
                "provider_intents": changeset.provider_intents,
            }
        )

    @staticmethod
    def _sync_snapshot(changeset: ChangeSet, content: ChangeSetContent) -> None:
        payload = _content_payload(content)
        changeset.basis_refs = payload["basis_refs"]
        changeset.expected_versions = payload["expected_versions"]
        changeset.registry_changes = payload["registry_changes"]
        changeset.preference_changes = payload["preference_changes"]
        changeset.lane_changes = payload["lane_changes"]
        changeset.event_changes = payload["event_changes"]
        changeset.resolution_changes = payload["resolution_changes"]
        changeset.provider_intents = payload["provider_intents"]

    def _revision(
        self,
        changeset: ChangeSet,
        content: ChangeSetContent,
        revision: int,
    ) -> ChangeSetRevision:
        payload = _content_payload(content)
        parameter_hash = sha256_json(payload)
        preview_hash = sha256_json(
            {
                key: payload[key]
                for key in (
                    "registry_changes",
                    "preference_changes",
                    "lane_changes",
                    "event_changes",
                    "resolution_changes",
                    "provider_intents",
                )
            }
        )
        item = ChangeSetRevision(
            change_set_id=changeset.id,
            revision=revision,
            basis_refs=payload["basis_refs"],
            expected_versions=payload["expected_versions"],
            registry_changes=payload["registry_changes"],
            preference_changes=payload["preference_changes"],
            lane_changes=payload["lane_changes"],
            event_changes=payload["event_changes"],
            resolution_changes=payload["resolution_changes"],
            provider_intents=payload["provider_intents"],
            parameter_hash=parameter_hash,
            preview_hash=preview_hash,
        )
        self.session.add(item)
        return item

    def _session_utterance_refs(self, intent_session: IntentSession) -> set[str]:
        refs = {intent_session.source_utterance_ref}
        refs.update(
            self.session.scalars(
                select(IntentTurn.utterance_ref).where(
                    IntentTurn.intent_session_id == intent_session.id
                )
            )
        )
        return refs

    def _validate(
        self,
        intent_session: IntentSession,
        content: ChangeSetContent,
        *,
        require_handlers: bool,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        provenance = ProvenanceRefService(self.session)
        try:
            provenance.require_all(content.basis_refs)
            authority_refs = provenance.authority_utterance_refs(content.basis_refs)
        except DocketError as exc:
            errors.append({"code": exc.code, "details": exc.details or {}})
            authority_refs = set()
        session_utterance_refs = self._session_utterance_refs(intent_session)
        if not authority_refs or not authority_refs.issubset(session_utterance_refs):
            errors.append(
                {
                    "code": "changeset_operator_authority_missing",
                    "details": {
                        "authority_utterance_refs": sorted(authority_refs),
                        "session_utterance_refs": sorted(session_utterance_refs),
                    },
                }
            )
        if intent_session.blocking_clarifications:
            errors.append(
                {
                    "code": "intent_needs_clarification",
                    "details": {"count": len(intent_session.blocking_clarifications)},
                }
            )

        changes = _all_changes(content)
        change_ids = {change.change_id for change in changes}
        seen_change_ids: set[str] = set()
        planned_refs: dict[str, str] = {}
        for change in changes:
            create_spec = change.create_spec or {}
            planned_ref = create_spec.get("ref_id")
            if planned_ref is None:
                continue
            expected_prefix = _OBJECT_PREFIXES[change.object_type]
            try:
                actual_prefix, _payload = parse_public_ref(str(planned_ref))
            except ValueError:
                actual_prefix = ""
            if actual_prefix != expected_prefix:
                errors.append(
                    {
                        "code": "planned_ref_type_mismatch",
                        "details": {
                            "change_id": change.change_id,
                            "ref": planned_ref,
                            "expected_prefix": expected_prefix,
                        },
                    }
                )
            elif str(planned_ref) in planned_refs:
                errors.append(
                    {
                        "code": "planned_ref_duplicate",
                        "details": {
                            "change_id": change.change_id,
                            "ref": planned_ref,
                        },
                    }
                )
            else:
                planned_refs[str(planned_ref)] = change.change_id
        create_lane_ids = {
            change.change_id
            for change in content.lane_changes
            if change.object_type == "calendar_lane" and change.action == "create"
        }
        for group_name, allowed_types in _GROUP_TYPES.items():
            for change in getattr(content, group_name):
                if change.object_type not in allowed_types:
                    errors.append(
                        {
                            "code": "changeset_group_type_mismatch",
                            "details": {
                                "change_id": change.change_id,
                                "group": group_name,
                                "object_type": change.object_type,
                            },
                        }
                    )
                try:
                    change_authority = provenance.authority_utterance_refs(change.basis_refs)
                except DocketError as exc:
                    errors.append(
                        {
                            "code": exc.code,
                            "details": {"change_id": change.change_id, **(exc.details or {})},
                        }
                    )
                    change_authority = set()
                if not change_authority or not change_authority.issubset(session_utterance_refs):
                    errors.append(
                        {
                            "code": "change_operator_authority_missing",
                            "details": {"change_id": change.change_id},
                        }
                    )
                if change.object_ref is not None:
                    expected_prefix = _OBJECT_PREFIXES[change.object_type]
                    actual_prefix, _payload = parse_public_ref(change.object_ref)
                    if actual_prefix != expected_prefix:
                        errors.append(
                            {
                                "code": "change_object_type_mismatch",
                                "details": {
                                    "change_id": change.change_id,
                                    "object_ref": change.object_ref,
                                    "expected_prefix": expected_prefix,
                                },
                            }
                        )
                    try:
                        target = provenance.get(change.object_ref)
                    except DocketError as exc:
                        errors.append(
                            {
                                "code": exc.code,
                                "details": {
                                    "change_id": change.change_id,
                                    **(exc.details or {}),
                                },
                            }
                        )
                    else:
                        expected_version = content.expected_versions.get(change.object_ref)
                        current_version = getattr(target, "version", None)
                        if expected_version is None:
                            errors.append(
                                {
                                    "code": "expected_version_required",
                                    "details": {
                                        "change_id": change.change_id,
                                        "object_ref": change.object_ref,
                                    },
                                }
                            )
                        elif current_version is None:
                            errors.append(
                                {
                                    "code": "object_not_versioned",
                                    "details": {"object_ref": change.object_ref},
                                }
                            )
                        elif current_version != expected_version:
                            errors.append(
                                {
                                    "code": "version_conflict",
                                    "details": {
                                        "object_ref": change.object_ref,
                                        "expected_version": expected_version,
                                        "current_version": current_version,
                                    },
                                }
                            )
                for referenced_ref in sorted(
                    _nested_public_refs(change.create_spec or change.payload)
                ):
                    planned_change_id = planned_refs.get(referenced_ref)
                    if planned_change_id is not None:
                        if planned_change_id not in seen_change_ids and (
                            planned_change_id != change.change_id
                        ):
                            errors.append(
                                {
                                    "code": "create_reference_order_invalid",
                                    "details": {
                                        "change_id": change.change_id,
                                        "referenced_change_id": planned_change_id,
                                    },
                                }
                            )
                        continue
                    try:
                        provenance.get(referenced_ref)
                    except DocketError as exc:
                        errors.append(
                            {
                                "code": "unresolved_required_reference",
                                "details": {
                                    "change_id": change.change_id,
                                    "ref": referenced_ref,
                                    "cause": exc.code,
                                },
                            }
                        )
                for referenced_change_id in sorted(
                    _nested_change_refs(change.create_spec or change.payload)
                ):
                    if referenced_change_id not in change_ids:
                        errors.append(
                            {
                                "code": "create_reference_unknown",
                                "details": {
                                    "change_id": change.change_id,
                                    "referenced_change_id": referenced_change_id,
                                },
                            }
                        )
                if change.object_type == "canonical_event":
                    formulation = (
                        change.create_spec if change.action == "create" else change.payload
                    )
                    formulation = formulation or {}
                    lane_ref = formulation.get("lane_ref")
                    lane_change_id = formulation.get("lane_change_id")
                    if lane_ref is None and lane_change_id not in create_lane_ids:
                        errors.append(
                            {
                                "code": "calendar_lane_unresolved",
                                "details": {"change_id": change.change_id},
                            }
                        )
                    elif lane_ref is not None:
                        try:
                            lane_prefix, _payload = parse_public_ref(str(lane_ref))
                            if lane_prefix != "lane":
                                raise ValueError
                            provenance.get(str(lane_ref))
                        except (DocketError, ValueError):
                            errors.append(
                                {
                                    "code": "invalid_calendar_lane",
                                    "details": {"change_id": change.change_id},
                                }
                            )
                if require_handlers and change.object_type not in self.handlers:
                    errors.append(
                        {
                            "code": "canonical_domain_not_migrated",
                            "details": {
                                "change_id": change.change_id,
                                "object_type": change.object_type,
                            },
                        }
                    )
                seen_change_ids.add(change.change_id)

        route_changes = [
            change
            for change in content.lane_changes
            if change.object_type == "lane_routing_decision" and change.action == "create"
        ]
        for event_change in content.event_changes:
            if event_change.object_type != "canonical_event":
                continue
            formulation = (
                event_change.create_spec
                if event_change.action == "create"
                else event_change.payload
            ) or {}
            event_ref = event_change.object_ref
            route_ref = formulation.get("routing_decision_ref")
            matching_route = next(
                (
                    route
                    for route in route_changes
                    if (route.create_spec or {}).get("event_change_id") == event_change.change_id
                    or (
                        event_ref is not None
                        and (route.create_spec or {}).get("event_ref") == event_ref
                    )
                ),
                None,
            )
            route_lane_ref: str | None = None
            route_lane_change_id: str | None = None
            if matching_route is not None:
                route_formulation = matching_route.create_spec or {}
                route_lane_ref = route_formulation.get("lane_ref")
                route_lane_change_id = route_formulation.get("lane_change_id")
            elif route_ref is not None:
                try:
                    existing_route = provenance.get(str(route_ref))
                except DocketError:
                    existing_route = None
                if isinstance(existing_route, LaneRoutingDecision):
                    route_lane_ref = existing_route.lane_ref
            else:
                errors.append(
                    {
                        "code": "lane_routing_decision_required",
                        "details": {"change_id": event_change.change_id},
                    }
                )
                continue
            event_lane_ref = formulation.get("lane_ref")
            event_lane_change_id = formulation.get("lane_change_id")
            if route_lane_ref != event_lane_ref or route_lane_change_id != event_lane_change_id:
                errors.append(
                    {
                        "code": "lane_routing_decision_mismatch",
                        "details": {"change_id": event_change.change_id},
                    }
                )

        for conflict in self.session.scalars(select(Conflict).where(Conflict.status == "open")):
            for change in changes:
                subject_refs = set(conflict.subject_refs)
                target_refs = {change.object_ref} if change.object_ref is not None else set()
                target = None
                if change.object_ref is not None:
                    try:
                        target = provenance.get(change.object_ref)
                    except DocketError:
                        target = None
                if isinstance(target, Fact | Affiliation):
                    target_entity = self.session.get(Entity, target.subject_entity_id)
                    if target_entity is not None:
                        target_refs.add(target_entity.ref_id)
                elif isinstance(target, Relationship):
                    for entity_id in (target.subject_entity_id, target.object_entity_id):
                        target_entity = self.session.get(Entity, entity_id)
                        if target_entity is not None:
                            target_refs.add(target_entity.ref_id)
                create_spec = change.create_spec or {}
                create_subjects = create_spec.get("subject_refs", [])
                target_refs.update(str(ref_id) for ref_id in create_subjects)
                for key in ("subject_ref", "object_ref", "entity_ref"):
                    if create_spec.get(key) is not None:
                        target_refs.add(str(create_spec[key]))
                if target_refs & subject_refs and set(change.affected_fields) & set(
                    conflict.affected_fields
                ):
                    errors.append(
                        {
                            "code": "open_conflict",
                            "details": {
                                "conflict_ref": conflict.ref_id,
                                "change_id": change.change_id,
                                "allowed_actions": [
                                    "resolve_conflict",
                                    "remove_blocked_mutation",
                                    "cancel_changeset",
                                ],
                            },
                        }
                    )

        for intent in content.provider_intents:
            try:
                intent_authority = provenance.authority_utterance_refs(intent.basis_refs)
            except DocketError as exc:
                errors.append(
                    {
                        "code": exc.code,
                        "details": {"intent_id": intent.intent_id, **(exc.details or {})},
                    }
                )
                intent_authority = set()
            if not intent_authority or not intent_authority.issubset(session_utterance_refs):
                errors.append(
                    {
                        "code": "provider_intent_operator_authority_missing",
                        "details": {"intent_id": intent.intent_id},
                    }
                )
            for target_ref in intent.canonical_target_refs:
                try:
                    provenance.get(target_ref)
                except DocketError as exc:
                    errors.append(
                        {
                            "code": exc.code,
                            "details": {"intent_id": intent.intent_id, **(exc.details or {})},
                        }
                    )
            missing_ids = set(intent.canonical_target_change_ids) - change_ids
            if missing_ids:
                errors.append(
                    {
                        "code": "provider_target_unresolved",
                        "details": {
                            "intent_id": intent.intent_id,
                            "change_ids": sorted(missing_ids),
                        },
                    }
                )
            if require_handlers and self.provider_handler is None:
                errors.append(
                    {
                        "code": "provider_intent_not_migrated",
                        "details": {"intent_id": intent.intent_id},
                    }
                )
        return errors

    @staticmethod
    def _clarifications(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "blocking": True,
                "code": error["code"],
                "details": error.get("details", {}),
            }
            for error in errors
        ]

    def _apply_validation(
        self,
        intent_session: IntentSession,
        changeset: ChangeSet,
        errors: list[dict[str, Any]],
    ) -> None:
        changeset.validation_errors = errors
        if errors:
            changeset.state = "draft"
            intent_session.state = "needs_clarification"
            existing = list(intent_session.blocking_clarifications)
            additional = self._clarifications(
                [error for error in errors if error["code"] != "intent_needs_clarification"]
            )
            existing_keys = {
                (
                    item.get("code"),
                    item.get("conflict_ref"),
                    str(item.get("details", {})),
                )
                for item in existing
            }
            intent_session.blocking_clarifications = [
                *existing,
                *[
                    item
                    for item in additional
                    if (
                        item.get("code"),
                        item.get("conflict_ref"),
                        str(item.get("details", {})),
                    )
                    not in existing_keys
                ],
            ]
        else:
            changeset.state = "validated"
            intent_session.state = "ready"
            intent_session.blocking_clarifications = []
        intent_session.version += 1

    def prepare(self, request: ChangeSetPrepare) -> tuple[ChangeSet, bool]:
        intent_session = IntentSessionService(self.session).get(request.intent_session_ref)
        if intent_session.version != request.expected_session_version:
            raise DocketError(
                code="version_conflict",
                message="IntentSession changed after it was read.",
                details={
                    "expected_version": request.expected_session_version,
                    "current_version": intent_session.version,
                },
            )
        if intent_session.state in {"committed", "cancelled", "superseded"}:
            raise DocketError(
                code="intent_session_closed",
                message="A closed IntentSession cannot compile another ChangeSet.",
            )
        existing = self.session.scalar(
            select(ChangeSet).where(ChangeSet.idempotency_key == request.idempotency_key)
        )
        payload_hash = sha256_json(_content_payload(request.content))
        if existing is not None:
            revision = self.session.scalar(
                select(ChangeSetRevision).where(
                    ChangeSetRevision.change_set_id == existing.id,
                    ChangeSetRevision.revision == existing.current_revision,
                )
            )
            if (
                existing.intent_session_id != intent_session.id
                or revision is None
                or revision.parameter_hash != payload_hash
            ):
                raise IdempotencyConflict(request.idempotency_key)
            return existing, False
        changeset = ChangeSet(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            idempotency_key=request.idempotency_key,
            state="draft",
            version=1,
            current_revision=1,
        )
        self._sync_snapshot(changeset, request.content)
        self.session.add(changeset)
        self.session.flush()
        self._revision(changeset, request.content, 1)
        errors = self._validate(intent_session, request.content, require_handlers=False)
        self._apply_validation(intent_session, changeset, errors)
        self.session.add(
            AuditEvent(
                event_type="changeset.compiled",
                entity_type="changeset",
                entity_id=changeset.id,
                actor_type="docket_compiler",
                actor_id=None,
                request_id=None,
                primary_ref=changeset.ref_id,
                affected_refs=[changeset.ref_id, intent_session.ref_id],
                basis_refs=changeset.basis_refs,
                data={
                    "revision": changeset.current_revision,
                    "state": changeset.state,
                    "validation_error_count": len(errors),
                },
            )
        )
        return changeset, True

    def revise(self, request: ChangeSetRevise) -> ChangeSet:
        changeset = self.get(request.changeset_ref)
        if changeset.state in {"committed", "cancelled", "superseded"}:
            raise DocketError(
                code="changeset_immutable",
                message="This ChangeSet can no longer be revised.",
            )
        if changeset.version != request.expected_version:
            raise DocketError(
                code="version_conflict",
                message="ChangeSet changed after it was read.",
                details={
                    "expected_version": request.expected_version,
                    "current_version": changeset.version,
                },
            )
        intent_session = self.session.get(IntentSession, changeset.intent_session_id)
        if intent_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="ChangeSet lost its IntentSession binding.",
            )
        next_revision = changeset.current_revision + 1
        self._sync_snapshot(changeset, request.content)
        changeset.current_revision = next_revision
        changeset.version += 1
        self._revision(changeset, request.content, next_revision)
        errors = self._validate(intent_session, request.content, require_handlers=False)
        self._apply_validation(intent_session, changeset, errors)
        self.session.add(
            AuditEvent(
                event_type="changeset.revised",
                entity_type="changeset",
                entity_id=changeset.id,
                actor_type="docket_compiler",
                actor_id=None,
                request_id=None,
                primary_ref=changeset.ref_id,
                affected_refs=[changeset.ref_id, intent_session.ref_id],
                basis_refs=changeset.basis_refs,
                data={
                    "revision": changeset.current_revision,
                    "state": changeset.state,
                    "validation_error_count": len(errors),
                },
            )
        )
        return changeset

    def commit(self, request: ChangeSetCommit) -> tuple[ChangeSet, list[str]]:
        changeset = self.get(request.changeset_ref)
        if changeset.idempotency_key != request.idempotency_key:
            raise IdempotencyConflict(request.idempotency_key)
        if changeset.state == "committed":
            return changeset, []
        if changeset.state != "validated":
            raise DocketError(
                code="changeset_not_resolved",
                message="Only a validated ChangeSet may commit.",
                details={"state": changeset.state},
            )
        if changeset.version != request.expected_version:
            raise DocketError(
                code="version_conflict",
                message="ChangeSet changed after it was read.",
                details={
                    "expected_version": request.expected_version,
                    "current_version": changeset.version,
                },
            )
        intent_session = self.session.get(IntentSession, changeset.intent_session_id)
        if intent_session is None or intent_session.state != "ready":
            raise DocketError(
                code="intent_not_resolved",
                message="ChangeSet IntentSession does not satisfy Resolved Intent.",
            )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == request.authority_utterance_ref
            )
        )
        if (
            utterance is None
            or utterance.actor_ref != f"discord_user:{get_settings().operator_discord_user_id}"
        ):
            raise DocketError(
                code="operator_utterance_authority_required",
                message="ChangeSet commit requires an authenticated OperatorUtterance.",
            )
        content = self._content(changeset)
        authority_refs = ProvenanceRefService(self.session).authority_utterance_refs(
            content.basis_refs
        )
        if request.authority_utterance_ref not in authority_refs:
            raise DocketError(
                code="changeset_authority_mismatch",
                message="Commit utterance is not a basis of this ChangeSet.",
            )
        errors = self._validate(intent_session, content, require_handlers=True)
        if errors:
            changeset.validation_errors = errors
            raise DocketError(
                code="changeset_validation_failed",
                message="ChangeSet failed deterministic pre-commit validation.",
                details={"errors": errors},
            )
        affected_refs: list[str] = []
        refs_by_change_id: dict[str, list[str]] = {}
        for change in _all_changes(content):
            resolved_change = change
            resolved_fields: dict[str, Any] = {}
            if change.create_spec is not None:
                resolved_fields["create_spec"] = _resolved_create_spec(
                    change.create_spec, refs_by_change_id
                )
            if change.payload:
                resolved_fields["payload"] = _resolved_create_spec(
                    change.payload, refs_by_change_id
                )
            if resolved_fields:
                resolved_change = change.model_copy(update=resolved_fields)
            refs = self.handlers[change.object_type](self.session, changeset, resolved_change)
            refs_by_change_id[change.change_id] = refs
            affected_refs.extend(refs)
        if self.provider_handler is not None:
            provider_intents = sorted(
                content.provider_intents,
                key=lambda item: (
                    0 if item.operation_type == "calendar_configure_lane" else 1,
                    item.intent_id,
                ),
            )
            for intent in provider_intents:
                affected_refs.extend(
                    self.provider_handler(
                        self.session,
                        changeset,
                        intent,
                        refs_by_change_id,
                    )
                )
        changeset.state = "committed"
        changeset.committed_at = utc_now()
        changeset.version += 1
        changeset.validation_errors = []
        intent_session.state = "committed"
        intent_session.committed_changeset_ref = changeset.ref_id
        intent_session.version += 1
        self.session.add(
            AuditEvent(
                event_type="changeset.committed",
                entity_type="changeset",
                entity_id=changeset.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                request_id=None,
                primary_ref=changeset.ref_id,
                affected_refs=[changeset.ref_id, intent_session.ref_id, *affected_refs],
                basis_refs=changeset.basis_refs,
                data={
                    "revision": changeset.current_revision,
                    "canonical_effect_count": len(affected_refs),
                },
            )
        )
        self.session.flush()
        return changeset, affected_refs

    def projection(self, changeset: ChangeSet) -> dict[str, Any]:
        return {
            "ref": changeset.ref_id,
            "state": changeset.state,
            "version": changeset.version,
            "intent_session_ref": self.session.scalar(
                select(IntentSession.ref_id).where(IntentSession.id == changeset.intent_session_id)
            ),
            "basis_refs": changeset.basis_refs,
            "current_revision": changeset.current_revision,
            "validation_errors": changeset.validation_errors,
            "counts": {
                "registry": len(changeset.registry_changes),
                "preferences": len(changeset.preference_changes),
                "lanes": len(changeset.lane_changes),
                "events": len(changeset.event_changes),
                "resolutions": len(changeset.resolution_changes),
                "provider_intents": len(changeset.provider_intents),
            },
            "committed_at": (
                changeset.committed_at.isoformat() if changeset.committed_at is not None else None
            ),
        }

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import CanonicalEvent, EventOccurrence, SemanticRequest
from docket.schemas.authority import mutation_input_json
from docket.schemas.event_occurrences import (
    CompiledOccurrenceEdit,
    EventMutationScope,
    OneTimeEventScope,
)


class EventScopeGuard:
    """The mutation service, not a prompt or a provider ID, owns series scope."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def require_bound_scope(
        self, event_ref: str, scope: EventMutationScope, semantic_request_ref: str | None
    ) -> None:
        request = (
            self.session.scalar(
                select(SemanticRequest).where(SemanticRequest.ref_id == semantic_request_ref)
            )
            if semantic_request_ref
            else None
        )
        binding = (request.selected_option_binding or {}) if request else {}
        authorized = binding.get("scope", {}).get("event_scopes", {}).get(event_ref)
        if authorized != scope.model_dump(mode="json", exclude_none=True):
            raise DocketError(
                code="event_scope_authority_mismatch",
                message="Event scope differs from the immutable semantic request binding.",
                details={"event_ref": event_ref, "next_action": "reconcile_semantic_scope"},
            )

    def validate(
        self,
        change: Any,
        *,
        semantic_request_ref: str | None,
        occurrence_plans: list[CompiledOccurrenceEdit],
    ) -> None:
        if change.object_ref is None:
            return
        event = self.session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == change.object_ref)
        )
        if event is None:
            return  # Ordinary existence validation supplies its own diagnostic.
        scope = getattr(change, "scope", OneTimeEventScope())
        recurring = bool(event.event_spec.get("recurrence"))
        patch = change.payload
        patch_spec = (
            patch.get("event_spec")
            if isinstance(patch, dict)
            else getattr(patch, "event_spec", None)
        )
        incoming_recurrence = (
            patch_spec.get("recurrence")
            if isinstance(patch_spec, dict)
            else getattr(patch_spec, "recurrence", None)
        )
        normalized = type(change).model_validate(mutation_input_json(change, warnings=False))
        payload_hash = sha256_json(mutation_input_json(normalized))
        occurrence = self.session.scalar(
            select(EventOccurrence).where(EventOccurrence.replacement_event_ref == event.ref_id)
        )
        if occurrence is not None and not any(
            item.action_hashes.get(change.change_id) == payload_hash for item in occurrence_plans
        ):
            raise DocketError(
                code="occurrence_scope_required",
                message="Edit this replacement using its original recurrence identity.",
                details={
                    "identity": occurrence.identity_json,
                    "next_action": "stage_occurrence_change",
                },
            )
        if scope.kind == "one_time":
            if recurring or incoming_recurrence:
                raise DocketError(
                    code="recurring_event_scope_required",
                    message="A series ref alone cannot authorize a dated or whole-series edit.",
                    details={"event_ref": event.ref_id, "next_action": "stage_exact_event_scope"},
                )
            return
        self.require_bound_scope(event.ref_id, scope, semantic_request_ref)
        if not recurring and not incoming_recurrence:
            raise DocketError(code="event_is_not_recurring", message="Target is not a series.")
        if scope.kind == "entire_series":
            return
        # Dependency resolution uses dictionaries and omits defaults. Restore
        # the typed representation before comparing with the pinned action.
        for compiled in occurrence_plans:
            if (
                compiled.source_scope == scope
                and compiled.action_hashes.get(change.change_id) == payload_hash
                and change.action == "update"
            ):
                # Only a compiler-produced exception update may address the master
                # under occurrence authority. A retract can NEVER pass this guard.
                return
        raise DocketError(
            code="occurrence_effect_not_compiled",
            message="An occurrence requires Docket's exact exception compilation.",
            details={"next_action": "stage_occurrence_change"},
        )

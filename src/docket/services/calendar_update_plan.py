"""Server-owned sparse Event updates and immutable provider preconditions.

The model supplies canonical changes, not a Google field mask. Diff the old and
new canonical representations before applying them, then pin the exact binding
observed by that draft. A provider edit does not erase the binding's identity.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import CalendarLane, CanonicalEvent, ProviderEventBinding
from docket.providers.google.calendar import CalendarEventRequest
from docket.services.calendar_projection_invariants import unavailable_event_binding

PATCH_FIELDS = frozenset({
    "summary", "description", "location", "start", "end", "recurrence", "transparency",
})


def _observation(binding: ProviderEventBinding) -> dict[str, Any]:
    return {
        "external_event_id": binding.provider_event_id,
        "provider_etag": binding.provider_etag,
        "binding_version": binding.version,
    }


def compile_event_update_plan(
    session: Session, event: CanonicalEvent, patch: dict[str, Any], lane: CalendarLane,
) -> dict[str, Any]:
    before = {**event.event_spec, "title": event.title}
    spec = patch.get("event_spec", event.event_spec)
    title = patch.get("title", event.title)
    if not isinstance(spec, dict) or not isinstance(title, str):
        raise DocketError(
            code="canonical_event_update_invalid",
            message="An Event update cannot clear its specification or title.",
            details={"target_ref": event.ref_id, "next_action": "repair_staged_actions"},
        )
    after = {**spec, "title": title}

    def body(spec: dict[str, Any]) -> dict[str, Any]:
        return CalendarEventRequest(
            calendar_id="", provider_correlation="", summary=spec["title"], event_spec=spec,
        ).event_body()

    old, new = body(before), body(after)
    fields = sorted(field for field in PATCH_FIELDS if old.get(field) != new.get(field))
    binding = session.scalar(select(ProviderEventBinding).where(
        ProviderEventBinding.canonical_target_ref == event.ref_id,
        ProviderEventBinding.target_kind == "event",
        ProviderEventBinding.account_id == lane.account_id,
        ProviderEventBinding.calendar_id == lane.calendar_id,
    ))
    return {
        "event_patch_fields": fields,
        "provider_observation": _observation(binding) if binding is not None else None,
    }


def validate_event_update_plan(
    session: Session, *, target_ref: str, lane: CalendarLane,
    parameters: dict[str, Any], lock: bool = False,
) -> ProviderEventBinding:
    query = select(ProviderEventBinding).where(
        ProviderEventBinding.canonical_target_ref == target_ref,
        ProviderEventBinding.target_kind == "event",
        ProviderEventBinding.account_id == lane.account_id,
        ProviderEventBinding.calendar_id == lane.calendar_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    binding = session.scalar(query)
    if binding is None or binding.status not in {"active", "diverged"}:
        raise unavailable_event_binding(session, target_ref=target_ref, target_kind="event")
    fields = parameters.get("event_patch_fields")
    observation = parameters.get("provider_observation")
    if (
        not isinstance(fields, list)
        or any(not isinstance(field, str) or field not in PATCH_FIELDS for field in fields)
        or not isinstance(observation, dict)
    ):
        raise DocketError(
            code="provider_event_patch_required",
            message="Restage the preserved Event change to bind its exact provider patch.",
            details={"target_ref": target_ref, "category": "provider_readiness",
                     "authority_preserved": True, "next_action": "restage_preserved_event_change"},
        )
    if not binding.provider_etag or observation != _observation(binding):
        raise DocketError(
            code="provider_event_version_conflict",
            message="The provider binding changed since staging; this patch was not applied.",
            details={"target_ref": target_ref, "category": "provider_readiness",
                     "binding_state": binding.status, "authority_preserved": True,
                     "next_action": "refresh_provider_evidence_then_restage_preserved_change"},
        )
    if binding.status == "diverged" and set(fields).intersection({"start", "end", "recurrence"}):
        # Timing/recurrence fields can encode other occurrences and exceptions.
        # Do not treat a whole-array replacement as a disjoint scalar repair.
        error = unavailable_event_binding(session, target_ref=target_ref, target_kind="event")
        assert error.details is not None
        error.details["next_action"] = "reconcile_provider_timing_or_recurrence"
        raise error
    return binding

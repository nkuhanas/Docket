from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import AuditEvent, CalendarLane, CanonicalEvent, ChangeSet
from docket.schemas.events import CanonicalEventCreateSpec, CanonicalEventPatchSpec

EventHandler = Callable[[Session, ChangeSet, Any], list[str]]


class CanonicalEventAuthorityService:
    """Apply provenance-complete CanonicalEvent changes inside one ChangeSet."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, EventHandler]:
        return {"canonical_event": self.apply_event}

    @staticmethod
    def _provenance(changeset: ChangeSet, change: Any) -> dict[str, Any]:
        return {
            "basis_refs": list(change.basis_refs),
            "decision_refs": [
                ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "dec"
            ],
            "source_refs": [
                ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "src"
            ],
            "created_by_changeset_ref": changeset.ref_id,
            "provenance_status": "complete",
        }

    def _lane(self, lane_ref: str) -> CalendarLane:
        lane = self.session.scalar(
            select(CalendarLane).where(CalendarLane.ref_id == lane_ref)
        )
        if lane is None or not lane.enabled:
            raise DocketError(
                code="calendar_lane_unavailable",
                message="CanonicalEvent requires an enabled CalendarLane.",
                details={"lane_ref": lane_ref},
            )
        return lane

    def apply_event(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        event: CanonicalEvent
        if change.action == "create":
            spec = CanonicalEventCreateSpec.model_validate(change.create_spec)
            if spec.lane_ref is None:
                raise DocketError(
                    code="create_reference_unresolved",
                    message="CanonicalEvent lane was not resolved before execution.",
                )
            lane = self._lane(spec.lane_ref)
            if spec.event_spec.calendar_lane != lane.lane:
                raise DocketError(
                    code="calendar_lane_mismatch",
                    message="Event specification lane does not match its canonical lane ref.",
                )
            canonical_key = spec.canonical_key or f"changeset:{changeset.ref_id}:{change.change_id}"
            existing = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.canonical_key == canonical_key)
            )
            if existing is not None:
                raise DocketError(
                    code="canonical_event_exists",
                    message="CanonicalEvent key already exists; update its exact public ref.",
                    details={"event_ref": existing.ref_id},
                )
            event = CanonicalEvent(
                canonical_key=canonical_key,
                title=spec.title,
                status=spec.status,
                event_spec=spec.event_spec.model_dump(mode="json"),
                reminder_plan=(
                    spec.event_spec.reminder_plan.model_dump(mode="json")
                    if spec.event_spec.reminder_plan is not None
                    else None
                ),
                calendar_lane=lane.lane,
                lane_id=lane.id,
                lane_ref=lane.ref_id,
                routing_decision_ref=spec.routing_decision_ref,
                entity_refs=[{"ref": ref} for ref in spec.entity_refs],
                context_labels=spec.context_labels,
                operator_policy_text=spec.operator_policy_text,
                authority="explicit_user",
                **self._provenance(changeset, change),
            )
            self.session.add(event)
            self.session.flush()
        else:
            if change.object_ref is None:
                raise DocketError(
                    code="canonical_event_ref_required",
                    message="CanonicalEvent mutation requires an exact public ref.",
                )
            stored_event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == change.object_ref)
            )
            if stored_event is None:
                raise DocketError(
                    code="canonical_event_not_found", message="CanonicalEvent was not found."
                )
            event = stored_event
            if change.action == "retract":
                event.status = "cancelled"
            elif change.action in {"update", "supersede"}:
                patch = CanonicalEventPatchSpec.model_validate(change.payload)
                values = patch.model_dump(exclude_unset=True)
                if "lane_ref" in values:
                    lane = self._lane(values.pop("lane_ref"))
                    event.lane_id = lane.id
                    event.lane_ref = lane.ref_id
                    event.calendar_lane = lane.lane
                if "event_spec" in values:
                    event_spec = patch.event_spec
                    assert event_spec is not None
                    if event.lane_ref is not None:
                        current_lane = self._lane(event.lane_ref)
                        if event_spec.calendar_lane != current_lane.lane:
                            raise DocketError(
                                code="calendar_lane_mismatch",
                                message=(
                                    "Event specification lane does not match its "
                                    "canonical lane ref."
                                ),
                            )
                    event.event_spec = event_spec.model_dump(mode="json")
                    event.reminder_plan = (
                        event_spec.reminder_plan.model_dump(mode="json")
                        if event_spec.reminder_plan is not None
                        else None
                    )
                    values.pop("event_spec")
                if "entity_refs" in values:
                    event.entity_refs = [
                        {"ref": ref} for ref in (values.pop("entity_refs") or [])
                    ]
                for key, value in values.items():
                    setattr(event, key, value)
            else:
                raise DocketError(
                    code="unsupported_canonical_event_action",
                    message="Unsupported CanonicalEvent action.",
                )
            event.basis_refs = list(change.basis_refs)
            event.decision_refs = self._provenance(changeset, change)["decision_refs"]
            event.source_refs = self._provenance(changeset, change)["source_refs"]
            event.version += 1
        self.session.add(
            AuditEvent(
                event_type="canonical_event.changed",
                entity_type="canonical_event",
                entity_id=event.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=event.ref_id,
                affected_refs=[event.ref_id, event.lane_ref] if event.lane_ref else [event.ref_id],
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "change_id": change.change_id,
                    "action": change.action,
                },
            )
        )
        return [event.ref_id]

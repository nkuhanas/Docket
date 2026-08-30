from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    EventItemLink,
    Item,
    Task,
    TemporalBinding,
)
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

    def _replace_item_links(
        self,
        event: CanonicalEvent,
        *,
        item_refs: list[str],
        temporal_binding_refs: list[str],
        basis_refs: list[str],
    ) -> None:
        links_by_item: dict[str, str | None] = {
            item_ref: None for item_ref in item_refs
        }
        for item_ref in item_refs:
            item = self.session.scalar(select(Item).where(Item.ref_id == item_ref))
            if item is None or item.canonical_status != "active":
                raise DocketError(
                    code="event_item_unavailable",
                    message="CanonicalEvent Item link requires an active Item.",
                    details={"item_ref": item_ref},
                )
        for temporal_binding_ref in temporal_binding_refs:
            binding = self.session.scalar(
                select(TemporalBinding).where(
                    TemporalBinding.ref_id == temporal_binding_ref
                )
            )
            if binding is None or binding.canonical_status != "active":
                raise DocketError(
                    code="event_temporal_binding_unavailable",
                    message="Realized TemporalBinding must be active.",
                    details={"temporal_binding_ref": temporal_binding_ref},
                )
            prefix, _payload = parse_public_ref(binding.subject_ref)
            if prefix == "item":
                item_ref = binding.subject_ref
            else:
                task = self.session.scalar(
                    select(Task).where(Task.ref_id == binding.subject_ref)
                )
                if task is None:
                    raise DocketError(
                        code="event_temporal_subject_unavailable",
                        message="Realized Task temporal subject was not found.",
                    )
                item_ref = task.item_ref
            if item_ref in links_by_item and links_by_item[item_ref] is not None:
                raise DocketError(
                    code="event_temporal_binding_ambiguous",
                    message="One Event may realize at most one Time per linked Item.",
                    details={"item_ref": item_ref},
                )
            self._require_temporal_compatibility(
                event=event,
                binding=binding,
            )
            links_by_item[item_ref] = temporal_binding_ref
        existing = list(
            self.session.scalars(
                select(EventItemLink).where(EventItemLink.event_ref == event.ref_id)
            )
        )
        for link in existing:
            self.session.delete(link)
        for item_ref, realized_temporal_ref in links_by_item.items():
            self.session.add(
                EventItemLink(
                    event_ref=event.ref_id,
                    item_ref=item_ref,
                    realizes_temporal_binding_ref=realized_temporal_ref,
                    basis_refs=basis_refs,
                )
            )

    @staticmethod
    def _require_temporal_compatibility(
        *,
        event: CanonicalEvent,
        binding: TemporalBinding,
    ) -> None:
        timing = event.event_spec.get("timing")
        if not isinstance(timing, dict):
            raise DocketError(
                code="event_temporal_bounds_incompatible",
                message="CanonicalEvent timing is unavailable for Time realization.",
            )
        value = binding.temporal_value
        value_kind = value.get("kind")
        timing_kind = timing.get("kind")
        compatible = False
        if value_kind == "date":
            expected = date.fromisoformat(str(value["date"]))
            actual = (
                date.fromisoformat(str(timing["start_date"]))
                if timing_kind == "all_day"
                else datetime.fromisoformat(str(timing["start_local"])).date()
                if timing_kind == "timed"
                else None
            )
            compatible = actual == expected
        elif value_kind == "datetime" and timing_kind == "timed":
            compatible = (
                datetime.fromisoformat(str(timing["start_local"]))
                == datetime.fromisoformat(str(value["local_datetime"]))
                and timing.get("timezone") == value.get("timezone")
            )
        elif value_kind == "date_interval" and timing_kind == "all_day":
            start = date.fromisoformat(str(value["start_date"]))
            end = date.fromisoformat(str(value["end_date"]))
            if bool(value.get("end_inclusive")):
                end += timedelta(days=1)
            compatible = (
                date.fromisoformat(str(timing["start_date"])) == start
                and date.fromisoformat(str(timing["end_date"])) == end
                and timing.get("timezone") == value.get("timezone")
            )
        elif value_kind == "datetime_interval" and timing_kind == "timed":
            compatible = (
                datetime.fromisoformat(str(timing["start_local"]))
                == datetime.fromisoformat(str(value["start_local"]))
                and datetime.fromisoformat(str(timing["end_local"]))
                == datetime.fromisoformat(str(value["end_local"]))
                and timing.get("timezone") == value.get("timezone")
            )
        if not compatible:
            raise DocketError(
                code="event_temporal_bounds_incompatible",
                message=(
                    "CanonicalEvent occurrence bounds do not realize the linked "
                    "TemporalBinding."
                ),
                details={
                    "event_ref": event.ref_id,
                    "temporal_binding_ref": binding.ref_id,
                },
            )

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
                lane_id=lane.id,
                lane_ref=lane.ref_id,
                routing_decision_ref=spec.routing_decision_ref,
                entity_refs=list(spec.entity_refs),
                context_labels=spec.context_labels,
                operator_policy_text=spec.operator_policy_text,
                authority="explicit_operator",
                **self._provenance(changeset, change),
            )
            self.session.add(event)
            self.session.flush()
            self._replace_item_links(
                event,
                item_refs=list(spec.item_refs),
                temporal_binding_refs=list(spec.realizes_temporal_binding_refs),
                basis_refs=list(change.basis_refs),
            )
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
                    values.pop("event_spec")
                if "entity_refs" in values:
                    event.entity_refs = list(values.pop("entity_refs") or [])
                if (
                    "item_refs" in values
                    or "realizes_temporal_binding_refs" in values
                ):
                    current_links = list(
                        self.session.scalars(
                            select(EventItemLink).where(
                                EventItemLink.event_ref == event.ref_id
                            )
                        )
                    )
                    item_refs = values.pop(
                        "item_refs", [link.item_ref for link in current_links]
                    )
                    temporal_refs = values.pop(
                        "realizes_temporal_binding_refs",
                        [
                            link.realizes_temporal_binding_ref
                            for link in current_links
                            if link.realizes_temporal_binding_ref is not None
                        ],
                    )
                    self._replace_item_links(
                        event,
                        item_refs=list(item_refs or []),
                        temporal_binding_refs=list(temporal_refs or []),
                        basis_refs=list(change.basis_refs),
                    )
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

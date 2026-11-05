"""Event creation plans with explicit destination and derived support records."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import CalendarLane, NativeContext
from docket.schemas.assembly import ScheduledOccurrenceEntry
from docket.schemas.authority import (
    CanonicalEventCreate,
    ItemCreate,
    LaneRoutingDecisionCreate,
    TemporalBindingCreate,
    mutation_input_json,
)
from docket.schemas.calendar import AllDayEventTiming
from docket.schemas.native_commands import CreateEvent
from docket.services.business_commands import mutation_plan
from docket.services.changeset_compiler import entry_facets
from docket.services.native_commands import event_timing


def create_event_plan(
    session: Session, context: NativeContext, command: CreateEvent, request_key: str
) -> dict[str, Any]:
    lane = session.scalar(select(CalendarLane).where(CalendarLane.ref_id == command.calendar_ref))
    if lane is None:
        raise DocketError(
            code="calendar_unavailable",
            message="Select a known calendar_ref.",
            details={"field": "calendar_ref"},
        )
    if command.all_day:
        timing: Any = AllDayEventTiming.model_validate(
            {
                "kind": "all_day",
                "start_date": command.start,
                "end_date": command.end,
                "timezone": command.timezone,
            }
        )
    else:
        timing = event_timing(command.start, command.end, command.timezone)
    entry = ScheduledOccurrenceEntry.model_validate(
        {
            "import_entry_id": "event",
            "title": command.title,
            "timing": timing,
            "lane_ref": lane.ref_id,
            "location": command.location,
            "description": command.notes,
            "context_entity_refs": command.entity_refs,
        }
    )
    item_spec, temporal_spec = entry_facets(entry)
    actions: list[Any] = []
    item_refs = list(command.item_refs)
    if command.time_refs and not item_refs:
        selected = {obj["ref"]: obj["state"] for obj in context.snapshot.get("objects", [])}
        for ref in command.time_refs:
            subject = selected.get(ref, {}).get("subject_ref")
            if (
                isinstance(subject, str)
                and subject.startswith("item_")
                and subject not in item_refs
            ):
                item_refs.append(subject)
        if not item_refs:
            raise DocketError(
                code="time_subject_context_required",
                message="Supply the linked item_refs when using existing time_refs.",
                details={"field": "item_refs"},
            )
    if not item_refs:
        actions.append(
            ItemCreate.model_validate(
                {
                    "change_id": "event.item",
                    "action": "create",
                    "object_type": "item",
                    "create_spec": item_spec.model_dump(mode="json"),
                    "affected_fields": ["item"],
                }
            )
        )
    if not command.time_refs:
        subject = (
            {"subject_ref": item_refs[0]} if item_refs else {"subject_change_id": "event.item"}
        )
        actions.append(
            TemporalBindingCreate.model_validate(
                {
                    "change_id": "event.time",
                    "action": "create",
                    "object_type": "temporal_binding",
                    "affected_fields": ["temporal_binding"],
                    "create_spec": {
                        **subject,
                        "role": temporal_spec.role,
                        "temporal_value": temporal_spec.temporal_value.model_dump(mode="json"),
                    },
                }
            )
        )
    identity = "native-event:" + sha256_json({"request": request_key})
    create_spec = {
        "title": command.title,
        "canonical_key": identity,
        "event_spec": {
            "title": command.title,
            "calendar_lane": lane.lane,
            "timing": entry.timing.model_dump(mode="json"),
            "location": command.location,
            "notes": command.notes,
            "operator_tags": command.tags,
            "recurrence": command.recurrence.model_dump(mode="json")
            if command.recurrence
            else None,
        },
        "lane_ref": lane.ref_id,
        "entity_refs": command.entity_refs,
        "context_labels": command.context_labels,
        "operator_policy_text": command.operator_policy_text,
        **({"item_refs": item_refs} if item_refs else {"item_change_ids": ["event.item"]}),
        **(
            {"realizes_temporal_binding_refs": command.time_refs}
            if command.time_refs
            else {"realizes_temporal_binding_change_ids": ["event.time"]}
        ),
    }
    actions.append(
        CanonicalEventCreate.model_validate(
            {
                "change_id": "event",
                "action": "create",
                "object_type": "canonical_event",
                "create_spec": create_spec,
                "affected_fields": ["canonical_event"],
            }
        )
    )
    actions.append(
        LaneRoutingDecisionCreate.model_validate(
            {
                "change_id": "event.route",
                "action": "create",
                "object_type": "lane_routing_decision",
                "affected_fields": ["lane", "route"],
                "create_spec": {
                    "lane_ref": lane.ref_id,
                    "event_change_id": "event",
                    "recurring_identity": identity,
                    "decision_kind": "explicit_operator",
                    "applicability_scope": {"request": request_key},
                    "operator_confirmed": True,
                },
            }
        )
    )
    return mutation_plan(
        context,
        [mutation_input_json(action, preserve_explicit_nulls=True) for action in actions],
        "create_event",
    )

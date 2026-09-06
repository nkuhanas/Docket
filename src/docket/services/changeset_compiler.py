from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.schemas.assembly import (
    NormalizedEntryInput,
    ScheduledOccurrenceEntry,
    ScheduleExceptionEntry,
)
from docket.schemas.authority import (
    CanonicalEventCreate,
    ImportEntryCoverage,
    ItemCreate,
    LaneRoutingDecisionCreate,
    TemporalBindingCreate,
)
from docket.schemas.calendar import AllDayEventTiming, TimedEventTiming
from docket.schemas.tracked_context import (
    DateIntervalTemporalValue,
    DateTimeIntervalTemporalValue,
    TemporalBindingInput,
)

COMPILER_IDENTIFIER = "docket.normalized-temporal-entry"
COMPILER_VERSION = 1


@dataclass(frozen=True)
class CompiledNormalizedEntry:
    stored_entry: dict[str, Any]
    actions: tuple[dict[str, Any], ...]
    ownership: dict[str, Any]
    coverage: ImportEntryCoverage
    predicted_provider_operation_types: tuple[str, ...]


def _assert_occurrence_matches_time(entry: ScheduledOccurrenceEntry) -> None:
    timing = entry.calendar.event_spec.timing
    value = entry.temporal.temporal_value
    if isinstance(timing, TimedEventTiming) and isinstance(value, DateTimeIntervalTemporalValue):
        if (
            timing.start_local != value.start_local
            or timing.end_local != value.end_local
            or timing.timezone != value.timezone
            or timing.fold != value.fold
        ):
            raise DocketError(
                code="normalized_occurrence_time_mismatch",
                message="The Event timing must exactly match the normalized temporal interval.",
            )
        return
    if isinstance(timing, AllDayEventTiming) and isinstance(value, DateIntervalTemporalValue):
        if (
            timing.start_date != value.start_date
            or timing.end_date != value.end_date
            or value.end_inclusive
            or timing.timezone != value.timezone
        ):
            raise DocketError(
                code="normalized_occurrence_time_mismatch",
                message="The all-day Event timing must match the normalized date interval.",
            )
        return
    raise DocketError(
        code="normalized_occurrence_time_incomplete",
        message=(
            "A scheduled occurrence requires an exact datetime or all-day interval; "
            "a date alone is not an Event."
        ),
    )


def compile_normalized_entry(
    entry: NormalizedEntryInput,
    *,
    utterance_ref: str,
    statement_ref: str,
) -> CompiledNormalizedEntry:
    """Expand resolved normalized meaning into mechanical canonical actions."""

    if isinstance(entry, ScheduledOccurrenceEntry):
        _assert_occurrence_matches_time(entry)
    if isinstance(entry, ScheduleExceptionEntry) and entry.calendar.kind != "none":
        raise DocketError(
            code="schedule_exception_event_forbidden",
            message="A schedule exception cannot compile into an ordinary Event.",
        )

    normalized_input = entry.model_dump(mode="json", exclude_none=True)
    normalized_input_hash = sha256_json(normalized_input)
    item_change_id = f"{entry.import_entry_id}.item"
    time_change_id = f"{entry.import_entry_id}.time"
    event_change_id = f"{entry.import_entry_id}.event"
    route_change_id = f"{entry.import_entry_id}.route"
    basis_refs = [utterance_ref, statement_ref]
    item_spec = entry.item.model_copy(update={"source_refs": [entry.evidence.source_ref]})
    item = ItemCreate(
        change_id=item_change_id,
        mutation_type="item_create",
        action="create",
        object_type="item",
        object_ref=None,
        create_spec=item_spec,
        payload=None,
        affected_fields=["item"],
        basis_refs=basis_refs,
    )
    temporal = TemporalBindingCreate(
        change_id=time_change_id,
        mutation_type="temporal_binding_create",
        action="create",
        object_type="temporal_binding",
        object_ref=None,
        create_spec=TemporalBindingInput(
            subject_change_id=item_change_id,
            role=entry.temporal.role,
            binding_key=entry.temporal.binding_key,
            temporal_value=entry.temporal.temporal_value,
            source_refs=[entry.evidence.source_ref],
        ),
        payload=None,
        affected_fields=["temporal_binding"],
        basis_refs=basis_refs,
    )
    actions: list[dict[str, Any]] = [
        item.model_dump(mode="json", exclude_none=True),
        temporal.model_dump(mode="json", exclude_none=True),
    ]
    predicted_provider_operations: tuple[str, ...] = ()
    calendar_change_id: str | None = None
    if isinstance(entry, ScheduledOccurrenceEntry):
        calendar_change_id = event_change_id
        event = CanonicalEventCreate(
            change_id=event_change_id,
            mutation_type="canonical_event_create",
            action="create",
            object_type="canonical_event",
            object_ref=None,
            create_spec={
                "canonical_key": f"normalized-entry:{normalized_input_hash}",
                "title": entry.item.title,
                "event_spec": entry.calendar.event_spec,
                "lane_ref": entry.calendar.lane_ref,
                "lane_change_id": entry.calendar.lane_change_id,
                "entity_refs": entry.calendar.entity_refs,
                "item_change_ids": [item_change_id],
                "realizes_temporal_binding_change_ids": [time_change_id],
                "context_labels": [entry.item.kind] if entry.item.kind is not None else [],
                "status": "active",
            },
            payload=None,
            affected_fields=["canonical_event"],
            basis_refs=basis_refs,
        )
        actions.append(event.model_dump(mode="json", exclude_none=True))
        route = LaneRoutingDecisionCreate(
            change_id=route_change_id,
            mutation_type="lane_routing_decision_create",
            action="create",
            object_type="lane_routing_decision",
            object_ref=None,
            create_spec={
                "lane_ref": entry.calendar.lane_ref,
                "lane_change_id": entry.calendar.lane_change_id,
                "event_change_id": event_change_id,
                "recurring_identity": f"normalized-entry:{normalized_input_hash}",
                "decision_kind": "explicit_operator",
                "applicability_scope": {"import_entry_id": entry.import_entry_id},
                "operator_confirmed": True,
            },
            payload=None,
            affected_fields=["lane", "route"],
            basis_refs=basis_refs,
        )
        actions.append(route.model_dump(mode="json", exclude_none=True))
        predicted_provider_operations = ("calendar_create_event",)

    change_ids = [str(action["change_id"]) for action in actions]
    stored_entry = {
        **normalized_input,
        "statement_ref": statement_ref,
        "compiler_identifier": COMPILER_IDENTIFIER,
        "compiler_version": COMPILER_VERSION,
        "normalized_input_hash": normalized_input_hash,
    }
    ownership = {
        "owner_kind": "normalized_entry",
        "owner_import_entry_id": entry.import_entry_id,
        "compiler_identifier": COMPILER_IDENTIFIER,
        "compiler_version": COMPILER_VERSION,
        "normalized_input_hash": normalized_input_hash,
        "change_ids": change_ids,
        "predicted_provider_operation_types": list(predicted_provider_operations),
    }
    return CompiledNormalizedEntry(
        stored_entry=stored_entry,
        actions=tuple(actions),
        ownership=ownership,
        coverage=ImportEntryCoverage(
            entry_id=entry.import_entry_id,
            item_change_id=item_change_id,
            temporal_binding_change_id=time_change_id,
            calendar_representation=(
                "canonical_event" if calendar_change_id is not None else "none"
            ),
            calendar_change_id=calendar_change_id,
        ),
        predicted_provider_operation_types=predicted_provider_operations,
    )

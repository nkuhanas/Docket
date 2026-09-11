from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.schemas.assembly import (
    NormalizedEntryInput,
    NormalizedTemporalFacet,
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
from docket.schemas.calendar import StandaloneCalendarEventInput, TimedEventTiming
from docket.schemas.tracked_context import (
    DateIntervalTemporalValue,
    DateTimeIntervalTemporalValue,
    ItemInput,
    TemporalBindingInput,
)

COMPILER_IDENTIFIER = "docket.normalized-temporal-entry"
COMPILER_VERSION = 2
NORMALIZED_INPUT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CompiledNormalizedEntry:
    stored_entry: dict[str, Any]
    actions: tuple[dict[str, Any], ...]
    ownership: dict[str, Any]
    coverage: ImportEntryCoverage
    predicted_provider_operation_types: tuple[str, ...]


def normalized_entry_record(entry: NormalizedEntryInput, *, statement_ref: str) -> dict[str, Any]:
    normalized_input = entry.model_dump(mode="json", exclude_none=True)
    return {
        **normalized_input,
        "statement_ref": statement_ref,
        "compiler_identifier": COMPILER_IDENTIFIER,
        "compiler_version": COMPILER_VERSION,
        "input_schema_version": NORMALIZED_INPUT_SCHEMA_VERSION,
        "normalized_input_hash": sha256_json(normalized_input),
    }


def entry_mutation_types(entry: NormalizedEntryInput) -> set[str]:
    types = {"item_create", "temporal_binding_create"}
    if isinstance(entry, ScheduledOccurrenceEntry):
        types.update({"canonical_event_create", "lane_routing_decision_create"})
    return types


def entry_action_ids(entry: NormalizedEntryInput) -> list[str]:
    suffixes = ["item", "time"]
    if isinstance(entry, ScheduledOccurrenceEntry):
        suffixes += ["event", "route"]
    return [f"{entry.import_entry_id}.{suffix}" for suffix in suffixes]


def entry_facets(entry: NormalizedEntryInput) -> tuple[ItemInput, NormalizedTemporalFacet]:
    """Derive support semantics from a single resolved occurrence definition."""
    if not isinstance(entry, ScheduledOccurrenceEntry):
        return entry.item, entry.temporal
    timing = entry.timing
    value = (
        DateTimeIntervalTemporalValue(
            kind="datetime_interval",
            start_local=timing.start_local,
            end_local=timing.end_local,
            timezone=timing.timezone,
            fold=timing.fold,
        )
        if isinstance(timing, TimedEventTiming)
        else DateIntervalTemporalValue(
            kind="date_interval",
            start_date=timing.start_date,
            end_date=timing.end_date,
            timezone=timing.timezone,
            end_inclusive=False,
        )
    )
    return (
        ItemInput(
            title=entry.title,
            kind=entry.kind,
            description=entry.description,
            context_entity_refs=entry.context_entity_refs,
        ),
        NormalizedTemporalFacet(role="window", temporal_value=value),
    )


def compile_normalized_entry(
    entry: NormalizedEntryInput,
    *,
    utterance_ref: str,
    statement_ref: str,
    calendar_lane: str | None = None,
) -> CompiledNormalizedEntry:
    """Expand resolved normalized meaning into mechanical canonical actions."""

    if isinstance(entry, ScheduledOccurrenceEntry) and calendar_lane is None:
        raise DocketError(
            code="normalized_entry_lane_unresolved",
            message="Resolve the selected lane before compiling this occurrence.",
            details={
                "entry_id": entry.import_entry_id,
                "field_path": ["lane_ref"],
                "next_action": "resolve_lane",
            },
        )
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
    item_input, temporal_input = entry_facets(entry)
    item_spec = item_input.model_copy(update={"source_refs": [entry.evidence.source_ref]})
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
            role=temporal_input.role,
            binding_key=temporal_input.binding_key,
            temporal_value=temporal_input.temporal_value,
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
        assert calendar_lane is not None
        calendar_change_id = event_change_id
        event = CanonicalEventCreate(
            change_id=event_change_id,
            mutation_type="canonical_event_create",
            action="create",
            object_type="canonical_event",
            object_ref=None,
            create_spec={
                "canonical_key": f"normalized-entry:{normalized_input_hash}",
                "title": entry.title,
                "event_spec": StandaloneCalendarEventInput(
                    title=entry.title,
                    timing=entry.timing,
                    location=entry.location,
                    notes=entry.description,
                    calendar_lane=calendar_lane,
                ),
                "lane_ref": entry.lane_ref,
                "lane_change_id": entry.lane_change_id,
                "entity_refs": entry.context_entity_refs,
                "item_change_ids": [item_change_id],
                "realizes_temporal_binding_change_ids": [time_change_id],
                "context_labels": [entry.kind] if entry.kind is not None else [],
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
                "lane_ref": entry.lane_ref,
                "lane_change_id": entry.lane_change_id,
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
    stored_entry = normalized_entry_record(entry, statement_ref=statement_ref)
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

"""Everyday calendar/task plans with server-derived support and occurrence identity."""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ValidationError

from docket.domain.errors import DocketError
from docket.models import NativeContext
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventModify,
    ItemCreate,
    TaskCreate,
    TaskModify,
    TemporalBindingCreate,
    TemporalBindingModify,
    TemporalBindingRetract,
    mutation_input_json,
)
from docket.schemas.native_commands import (
    CancelEvent,
    CompleteTask,
    CreateTask,
    UpdateEvent,
    UpdateTask,
)
from docket.schemas.tracked_context import DateTimeTemporalValue
from docket.services.business_commands import mutation_plan, public_validation_error
from docket.services.native_commands import NativeCommandService, event_timing


def context_object(context: NativeContext, ref: str) -> dict[str, Any]:
    for obj in context.snapshot.get("objects", []):
        if obj["ref"] == ref:
            return dict(obj["state"])
    for obj in context.snapshot.get("canonical_events", []):
        if obj["event_ref"] == ref:
            return {
                "event_spec": {"timing": obj["timing"]},
                "recurring": obj.get("recurring", False),
            }
    raise DocketError(
        code="target_not_in_context",
        message="Read context for the selected target first.",
        details={"field": "context_id", "ref": ref, "next_action": "docket_get_context"},
    )


def occurrence_scope(values: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    scope = values.get("scope", "event")
    spec = state.get("event_spec", {})
    recurring = bool(spec.get("recurrence") or state.get("recurring"))
    if scope == "series":
        return {"kind": "entire_series"}
    if scope == "event":
        if recurring:
            raise DocketError(
                code="recurring_scope_required",
                message="Choose scope=series or scope=occurrence for this event.",
                details={"field": "scope"},
            )
        return {"kind": "one_time"}
    timing = spec.get("timing", {})
    timezone = timing.get("timezone")
    if not timezone:
        raise DocketError(
            code="event_timing_context_required",
            message="Read the event's object context before editing an occurrence.",
            details={"field": "context_id"},
        )
    identity: dict[str, Any] = {"series_ref": values["event_ref"], "timezone": timezone}
    if timing.get("kind") == "all_day":
        if not values.get("occurrence_date"):
            raise DocketError(
                code="occurrence_date_required",
                message="Supply the original date of the all-day occurrence.",
                details={"field": "occurrence_date"},
            )
        identity["original_date"] = values["occurrence_date"]
    else:
        supplied = values.get("occurrence_start")
        if not supplied:
            raise DocketError(
                code="occurrence_start_required",
                message="Supply the original occurrence's ISO start including offset.",
                details={"field": "occurrence_start"},
            )
        try:
            original = datetime.fromisoformat(supplied)
            if original.tzinfo is None:
                raise ValueError("offset required")
            zoned = original.astimezone(ZoneInfo(timezone))
            if original.utcoffset() != zoned.utcoffset():
                raise ValueError("timezone offset mismatch")
        except ValueError as exc:
            raise DocketError(
                code="invalid_occurrence_start",
                message="Use the original occurrence's local start and UTC offset.",
                details={"field": "occurrence_start"},
            ) from exc
        identity.update(
            original_date=zoned.date().isoformat(),
            original_start_local=zoned.replace(tzinfo=None).isoformat(),
            fold=zoned.fold,
        )
    return {"kind": "occurrence", "identity": identity}


def time_value(value: str, timezone: str) -> dict[str, Any]:
    try:
        zone = ZoneInfo(timezone)
        if len(value) == 10:
            return {
                "kind": "date",
                "date": date.fromisoformat(value).isoformat(),
                "timezone": timezone,
            }
        parsed = datetime.fromisoformat(value)
        fold = None
        if parsed.tzinfo:
            local = parsed.astimezone(zone)
            if local.utcoffset() != parsed.utcoffset():
                raise ValueError("timestamp offset must match timezone")
            parsed, fold = local.replace(tzinfo=None), local.fold
        return DateTimeTemporalValue(
            kind="datetime", local_datetime=parsed, timezone=timezone, fold=fold
        ).model_dump(mode="json")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise DocketError(
            code="invalid_due_time",
            message="Supply an ISO date or local timestamp with a matching timezone.",
            details={"field": "due"},
        ) from exc


class RoutineCommandService(NativeCommandService):
    def event(
        self, command: UpdateEvent | CancelEvent, request_ref: str, request_key: str
    ) -> dict[str, Any]:
        values = command.model_dump(mode="json", exclude_unset=True)
        cancel = isinstance(command, CancelEvent)
        name = "cancel_event" if cancel else "update_event"

        def build(context: NativeContext) -> dict[str, Any]:
            state = context_object(context, command.event_ref)
            scope = occurrence_scope(values, state)
            payload: dict[str, Any] = {}
            if not cancel:
                fields: dict[str, Any] = {
                    field: values[field]
                    for field in ("title", "location", "notes")
                    if field in values
                }
                if any(field in values for field in ("start", "end", "timezone")):
                    observed = state.get("event_spec", {}).get("timing", {})
                    if observed.get("kind") != "timed" and not all(
                        key in values for key in ("start", "end", "timezone")
                    ):
                        raise DocketError(
                            code="event_time_fields_required",
                            message="Supply start, end and timezone for this event.",
                            details={"field": "start"},
                        )
                    timezone = values.get("timezone", observed.get("timezone"))
                    start = values.get("start", observed.get("start_local"))
                    end = values.get("end", observed.get("end_local"))
                    # Offset-free observed times preserve their stored DST disambiguation.
                    for field, value in (("start", start), ("end", end)):
                        if field not in values and observed.get("fold"):
                            value = (
                                datetime.fromisoformat(value)
                                .replace(tzinfo=ZoneInfo(timezone), fold=observed["fold"])
                                .isoformat()
                            )
                            if field == "start":
                                start = value
                            else:
                                end = value
                    fields["timing"] = event_timing(start, end, timezone)
                if fields:
                    payload["event_spec"] = fields
                if "calendar_ref" in values:
                    payload["lane_ref"] = values["calendar_ref"]
                if "recurrence" in values:
                    payload["recurrence"] = values["recurrence"]
                if not payload:
                    raise DocketError(
                        code="no_event_changes", message="Supply the fields to change."
                    )
            try:
                mutation = (
                    CanonicalEventCancel if cancel else CanonicalEventModify
                ).model_validate(
                    {
                        "change_id": "event",
                        "object_type": "canonical_event",
                        "action": "retract"
                        if cancel
                        else ("supersede" if values.get("replace") else "update"),
                        "object_ref": command.event_ref,
                        "scope": scope,
                        "payload": payload,
                        "affected_fields": list(payload.get("event_spec", {}))
                        + [key for key in payload if key != "event_spec"]
                        or ["status"],
                    }
                )
            except ValidationError as exc:
                raise public_validation_error(exc) from exc
            return mutation_plan(
                context, [mutation_input_json(mutation, preserve_explicit_nulls=True)], name
            )

        return self.execute(
            command=name,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

    def task(
        self, command: CreateTask | UpdateTask | CompleteTask, request_ref: str, request_key: str
    ) -> dict[str, Any]:
        values = command.model_dump(mode="json", exclude_unset=True)
        create = isinstance(command, CreateTask)
        name = "create_task" if create else "update_task"

        def build(context: NativeContext) -> dict[str, Any]:
            actions: list[BaseModel] = []
            task_ref = command.task_ref if isinstance(command, UpdateTask | CompleteTask) else ""
            payload = {
                ({"notes": "description", "state": "task_state"}.get(key, key)): value
                for key, value in values.items()
                if key
                in {
                    "title",
                    "notes",
                    "state",
                    "priority",
                    "item_ref",
                    "canonical_status",
                    "completed_at",
                    "source_refs",
                }
            }
            if isinstance(command, CompleteTask):
                payload["task_state"] = "completed"
            if payload.get("task_state") == "completed":
                payload["completed_at"] = (
                    payload.get("completed_at") or datetime.now(UTC).isoformat()
                )
            elif payload.get("task_state"):
                payload["completed_at"] = None
            if isinstance(command, CreateTask) and not command.item_ref:
                actions.append(
                    ItemCreate.model_validate(
                        {
                            "change_id": "item",
                            "object_type": "item",
                            "action": "create",
                            "affected_fields": ["title", "description"],
                            "create_spec": {
                                "title": command.title,
                                "description": command.notes,
                                "kind": "work.task",
                            },
                        }
                    )
                )
                payload["item_change_id"] = "item"
            try:
                if create:
                    actions.append(
                        TaskCreate.model_validate(
                            {
                                "change_id": "task",
                                "action": "create",
                                "object_type": "task",
                                "affected_fields": list(payload),
                                "create_spec": payload,
                            }
                        )
                    )
                else:
                    context_object(context, task_ref)
                    if payload:
                        actions.append(
                            TaskModify.model_validate(
                                {
                                    "change_id": "task",
                                    "action": "update",
                                    "object_type": "task",
                                    "object_ref": task_ref,
                                    "affected_fields": list(payload),
                                    "payload": payload,
                                }
                            )
                        )
                if "due" in values:
                    existing = (
                        []
                        if create
                        else [
                            obj
                            for obj in context.snapshot.get("objects", [])
                            if obj["type"] == "temporal_binding"
                            and obj["state"].get("subject_ref") == task_ref
                            and obj["state"].get("role") == "due_by"
                            and obj["state"].get("canonical_status") == "active"
                        ]
                    )
                    if len(existing) > 1:
                        raise DocketError(
                            code="multiple_due_times",
                            message="Select the due-time entry to change explicitly.",
                        )
                    if existing:
                        target = existing[0]["ref"]
                        if values["due"] is None:
                            actions.append(
                                TemporalBindingRetract.model_validate(
                                    {
                                        "change_id": "due",
                                        "action": "retract",
                                        "object_type": "temporal_binding",
                                        "object_ref": target,
                                        "affected_fields": ["canonical_status"],
                                    }
                                )
                            )
                        else:
                            actions.append(
                                TemporalBindingModify.model_validate(
                                    {
                                        "change_id": "due",
                                        "action": "update",
                                        "object_type": "temporal_binding",
                                        "object_ref": target,
                                        "affected_fields": ["temporal_value"],
                                        "payload": {
                                            "temporal_value": time_value(
                                                values["due"],
                                                values.get("timezone") or self.settings.timezone,
                                            )
                                        },
                                    }
                                )
                            )
                    elif values["due"] is not None:
                        actions.append(
                            TemporalBindingCreate.model_validate(
                                {
                                    "change_id": "due",
                                    "action": "create",
                                    "object_type": "temporal_binding",
                                    "affected_fields": ["role", "temporal_value"],
                                    "create_spec": {"subject_change_id": "task"}
                                    | {
                                        "role": "due_by",
                                        "temporal_value": time_value(
                                            values["due"],
                                            values.get("timezone") or self.settings.timezone,
                                        ),
                                    },
                                }
                            )
                            if create
                            else TemporalBindingCreate.model_validate(
                                {
                                    "change_id": "due",
                                    "action": "create",
                                    "object_type": "temporal_binding",
                                    "affected_fields": ["role", "temporal_value"],
                                    "create_spec": {
                                        "subject_ref": task_ref,
                                        "role": "due_by",
                                        "temporal_value": time_value(
                                            values["due"],
                                            values.get("timezone") or self.settings.timezone,
                                        ),
                                    },
                                }
                            )
                        )
                if not actions:
                    raise DocketError(
                        code="no_task_changes", message="Supply the fields to change."
                    )
            except ValidationError as exc:
                raise public_validation_error(exc) from exc
            return mutation_plan(
                context,
                [mutation_input_json(action, preserve_explicit_nulls=True) for action in actions],
                name,
            )

        return self.execute(
            command=name,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

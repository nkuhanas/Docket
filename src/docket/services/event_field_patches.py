"""Materialize sparse event patches against canonical, versioned baselines."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import CanonicalEvent, EventOccurrence
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventModify,
    MaterializedEventModify,
)
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.events import EventFieldPatch


def patch_error(
    change: CanonicalEventModify | CanonicalEventCancel,
    code: str,
    path: list[str],
    next_action: str,
) -> DocketError:
    return DocketError(
        code=code,
        message="The selected event patch cannot be applied as staged.",
        details={
            "change_id": change.change_id,
            "field_path": path,
            "constraint": code,
            "next_action": next_action,
            "authority_preserved": True,
        },
    )


def merge_fields(baseline: dict[str, Any], patch: EventFieldPatch) -> dict[str, Any]:
    # Presence, not truthiness, distinguishes unchanged / clear / empty text.
    result = {**baseline, **patch.model_dump(mode="json", exclude_unset=True)}
    return StandaloneCalendarEventInput.model_validate(
        result,
        context={"allow_explicit_priority": True},
    ).model_dump(mode="json")


def materialize_event_patch(
    session: Session,
    change: CanonicalEventModify,
    versions: dict[str, int],
) -> MaterializedEventModify:
    event = session.scalar(
        select(CanonicalEvent).where(
            CanonicalEvent.ref_id == change.object_ref,
        )
    )
    if event is None:
        raise patch_error(change, "canonical_event_not_found", ["object_ref"], "read_exact_event")
    if versions.get(event.ref_id) != event.version:
        raise patch_error(
            change,
            "version_conflict",
            ["expected_versions", event.ref_id],
            "reconcile_event_version",
        )
    if event.status != "active" and change.payload.event_spec is not None:
        raise patch_error(
            change, "event_target_unavailable", ["object_ref"], "resolve_event_lifecycle"
        )
    values = change.payload.model_dump(mode="json", exclude_unset=True)
    spec = dict(event.event_spec)
    fields: list[str] = []
    if change.payload.event_spec is not None:
        spec = merge_fields(spec, change.payload.event_spec)
        fields.extend(
            f"event_spec.{key}"
            for key in change.payload.event_spec.model_fields_set
            if spec.get(key) != event.event_spec.get(key)
        )
    if "recurrence" in values:
        spec["recurrence"] = values.pop("recurrence")
        spec = StandaloneCalendarEventInput.model_validate(
            spec,
            context={"allow_explicit_priority": True},
        ).model_dump(mode="json")
        if spec.get("recurrence") != event.event_spec.get("recurrence"):
            fields.append("event_spec.recurrence")
    if "event_spec" in values or "recurrence" in change.payload.model_fields_set:
        values["event_spec"] = spec
        # There is one public title input; canonical/provider title stay in sync.
        if change.payload.event_spec is not None and (
            "title" in change.payload.event_spec.model_fields_set
        ):
            values["title"] = spec["title"]
            if event.title != spec["title"]:
                fields.append("title")
    for key, value in values.items():
        if key not in {"event_spec", "title"} and getattr(event, key, object()) != value:
            fields.append(key)
    overrides = 0
    if change.scope.kind == "entire_series":
        overrides = (
            session.scalar(
                select(func.count())
                .select_from(EventOccurrence)
                .where(
                    EventOccurrence.series_ref == event.ref_id,
                )
            )
            or 0
        )
    return MaterializedEventModify(
        change_id=change.change_id,
        action=change.action,
        object_type="canonical_event",
        object_ref=change.object_ref,
        scope=change.scope,
        payload=values,
        source_patch=change,
        affected_fields=sorted(set(fields)),
        basis_refs=change.basis_refs,
        no_op=not fields,
        preserved_override_count=overrides,
    )

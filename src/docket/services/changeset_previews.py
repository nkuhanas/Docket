"""Capture scoped planned Calendar effects at staging, never while paging a diff.

These snapshots are informational, not authority or a substitute for optimistic
version checks. No provider reads, writes, or canonical mutation occur here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.models import CanonicalEvent
from docket.schemas.authority import CanonicalEventCreate, ChangeSetContent
from docket.schemas.event_occurrences import CompiledOccurrenceEdit
from docket.services.event_occurrences import EventOccurrenceService

_SPEC_FIELDS = (
    "timing", "location", "notes", "recurrence", "operator_tags", "priority", "calendar_lane",
)


def _project(values: dict[str, Any]) -> dict[str, Any]:
    spec = values.get("event_spec") or {}
    result = {
        **{key: deepcopy(spec.get(key)) for key in _SPEC_FIELDS},
        **{key: deepcopy(values.get(key)) for key in ("title", "status", "lane_ref")},
    }
    # Surface a disagreement rather than hiding a title that a provider would use.
    if spec.get("title") != values.get("title"):
        result["calendar_title"] = spec.get("title")
    return result


def _stored(event: CanonicalEvent) -> dict[str, Any]:
    return {
        "title": event.title, "status": event.status,
        "lane_ref": event.lane_ref, "event_spec": deepcopy(event.event_spec),
    }


def _occurrence(
    session: Session, edit: CompiledOccurrenceEdit,
    events: dict[str, CanonicalEvent],
) -> dict[str, Any]:
    plan = edit.plan
    series = events.get(plan.identity.series_ref)
    header: dict[str, Any] = {
        "change_id": edit.source_change_id,
        "mutation_type": edit.source_change["mutation_type"],
        "scope": edit.source_scope.model_dump(mode="json"),
        "occurrence_identity": plan.identity.model_dump(mode="json"),
        "target_ref": plan.identity.series_ref,
        "expected_version": plan.series_version,
        "no_op": plan.no_op,
    }
    if series is None:
        return {**header, "available": False, "reason": "canonical_target_unavailable"}
    prior = EventOccurrenceService(session).get(plan.identity)
    child = events.get(plan.replacement_event_ref or "")
    original = _stored(child or series)
    if child is None:
        original["event_spec"].update({"timing": plan.original_timing, "recurrence": None})
    if prior is not None and prior.status == "cancelled":
        original["status"] = "cancelled"
    after = deepcopy(original)
    if plan.status == "cancelled":
        after["status"] = "cancelled"
    elif plan.replacement_after is not None:
        after.update({
            "title": plan.replacement_after["title"],
            "event_spec": plan.replacement_after, "status": "active",
        })
    return {
        **header, "available": True, "observed_version": series.version,
        "observed_occurrence_version": prior.version if prior else None,
        "expected_occurrence_version": plan.occurrence_version,
        "before": _project(original), "after": _project(after),
    }


def capture_event_preview(session: Session, content: ChangeSetContent | None) -> dict[str, Any]:
    if content is None:
        return {"format_version": 1, "available": False, "reason": "uncompiled_draft"}
    refs = {
        change.object_ref for change in content.event_changes
        if not isinstance(change, CanonicalEventCreate)
    }
    for edit in content.occurrence_plans:
        refs.add(edit.plan.identity.series_ref)
        if edit.plan.replacement_event_ref is not None:
            refs.add(edit.plan.replacement_event_ref)
    events = {
        event.ref_id: event for event in session.scalars(
            select(CanonicalEvent).where(CanonicalEvent.ref_id.in_(sorted(refs)))
        )
    }
    effects = [_occurrence(session, edit, events) for edit in content.occurrence_plans]
    owned = {
        change_id for edit in content.occurrence_plans for change_id in edit.action_hashes
    }
    for change in content.event_changes:
        if change.change_id in owned:
            continue
        scope = getattr(change, "scope", None)
        header: dict[str, Any] = {
            "change_id": change.change_id, "mutation_type": change.mutation_type,
            "scope": scope.model_dump(mode="json") if scope is not None else {"kind": "one_time"},
        }
        if header["scope"]["kind"] == "occurrence":
            effects.append({
                **header, "available": False, "reason": "occurrence_compilation_required",
            })
            continue
        if isinstance(change, CanonicalEventCreate):
            values = change.create_spec.model_dump(mode="json")
            header["scope"] = {
                "kind": "entire_series" if values["event_spec"].get("recurrence") else "one_time",
            }
            after = _project(values)
            if change.create_spec.lane_change_id is not None:
                after["lane_change_id"] = change.create_spec.lane_change_id
            effects.append({**header, "available": True, "before": None, "after": after})
            continue
        header.update({
            "target_ref": change.object_ref,
            "expected_version": content.expected_versions.get(change.object_ref),
        })
        event = events.get(change.object_ref)
        if event is None:
            effects.append({
                **header, "available": False, "reason": "canonical_target_unavailable",
            })
            continue
        before = _stored(event)
        after_values = deepcopy(before)
        if change.action == "retract":
            after_values["status"] = "cancelled"
        else:
            after_values.update(change.payload.model_dump(mode="json", exclude_unset=True))
            other = sorted(set(change.payload.model_fields_set) - {
                "title", "status", "lane_ref", "event_spec",
            })
            if other:
                header["additional_input_fields"] = other
        after = _project(after_values)
        if after_values.get("lane_change_id") is not None:
            after["lane_ref"] = None
            after["lane_change_id"] = after_values["lane_change_id"]
        effects.append({
            **header, "available": True, "observed_version": event.version,
            "before": _project(before), "after": after,
        })
    return {
        "format_version": 1, "available": True, "effects": effects,
        "projection_kind": "calendar_presentation",
        "effect_count": len(effects), "sha256": sha256_json(effects),
    }


def event_preview_sample(
    snapshot: dict[str, Any], *, entry_owned_ids: set[str], budget: int,
) -> dict[str, Any]:
    """Compact stage output; full before/after details remain in the revision diff."""
    effects = snapshot.get("effects", [])
    manual_effects = [effect for effect in effects if effect["change_id"] not in entry_owned_ids]
    sample: list[dict[str, Any]] = []
    for effect in manual_effects[:3]:
        after = effect.get("after") or {}
        row = {
            key: value for key, value in {
                "change_id": effect["change_id"], "target_ref": effect.get("target_ref"),
                "scope": effect["scope"], "no_op": effect.get("no_op"),
                "available": effect["available"],
                **{key: after.get(key) for key in (
                    "title", "status", "timing", "location", "lane_ref", "calendar_lane",
                )},
            }.items() if value is not None
        }
        if len(json.dumps([*sample, row], ensure_ascii=False).encode()) > budget:
            break
        sample.append(row)
    return {
        "event_preview": sample, "event_effect_count": len(effects),
        "event_effects_represented_by_entries": len(effects) - len(manual_effects),
        "omitted_event_effect_count": len(manual_effects) - len(sample),
        "event_preview_available": snapshot.get("available", False),
        "event_preview_basis": "canonical_staging_snapshot",
    }

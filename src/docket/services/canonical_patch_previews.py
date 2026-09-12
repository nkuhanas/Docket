"""Scoped canonical-before/planned-after fields for non-Event draft mutations.

Only fields touched by a typed patch are captured. This is a presentation
snapshot, not a second mutation compiler or authority validator. Adds already
have their complete proposed values in the input diff; correlation may reuse
an existing object at commit, so this module does not claim a new row exists.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.models import (
    Affiliation,
    AttentionCase,
    AttentionCaseRevision,
    CalendarLane,
    CaseItem,
    Entity,
    Fact,
    IdentityHandle,
    Item,
    Preference,
    Relationship,
    ReminderPlan,
    SenderIdentityEmail,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.schemas.authority import AttentionCaseResolutionInput, ChangeSetContent
from docket.schemas.policy import CalendarLanePatchSpec, PreferencePatchSpec
from docket.schemas.registry import AssertionUpdateSpec, EntityPatchSpec
from docket.schemas.tracked_context import (
    ItemPatchInput,
    ReminderPlanPatchInput,
    TaskPatchInput,
    TemporalBindingPatchInput,
    TemporalCalendarProjectionPatchInput,
)

# Deliberate domain field maps, not ORM serialization or UUID-prefix inference.
_RULES: dict[str, tuple[type[Any], type[BaseModel], str, Any]] = {
    "entity": (Entity, EntityPatchSpec, "canonical_status", "retracted"),
    "item": (Item, ItemPatchInput, "canonical_status", "retracted"),
    "task": (Task, TaskPatchInput, "canonical_status", "retracted"),
    "temporal_binding": (
        TemporalBinding, TemporalBindingPatchInput, "canonical_status", "retracted",
    ),
    "temporal_calendar_projection": (
        TemporalCalendarProjection, TemporalCalendarProjectionPatchInput, "enabled", False,
    ),
    "reminder_plan": (ReminderPlan, ReminderPlanPatchInput, "canonical_status", "retracted"),
    "preference": (Preference, PreferencePatchSpec, "status", "retracted"),
    "calendar_lane": (CalendarLane, CalendarLanePatchSpec, "enabled", False),
    "affiliation": (Affiliation, AssertionUpdateSpec, "status", "retracted"),
    "relationship": (Relationship, AssertionUpdateSpec, "status", "retracted"),
    "fact": (Fact, AssertionUpdateSpec, "status", "retracted"),
}
_SUPERSEDING = {"temporal_binding", "affiliation", "relationship", "fact"}
_GROUPS = ("registry_changes", "preference_changes", "lane_changes", "tracked_context_changes")


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return deepcopy(value)


def _identity_effect(session: Session, change: Any, handle: IdentityHandle) -> dict[str, Any]:
    if change.action in {"update", "supersede"}:
        refs = sorted(session.scalars(select(IdentityHandle.ref_id).join(
            SenderIdentityEmail,
            SenderIdentityEmail.email_identity_handle_id == IdentityHandle.id,
        ).where(
            SenderIdentityEmail.sender_identity_handle_id == handle.id,
            SenderIdentityEmail.status == "active",
        )))
        payload = change.payload.model_dump(mode="json", exclude_unset=True)
        after_refs = set(refs)
        if payload.get("add_associated_email_ref"):
            after_refs.add(payload["add_associated_email_ref"])
        if payload.get("remove_associated_email_ref"):
            after_refs.discard(payload["remove_associated_email_ref"])
        return {"before": {"associated_email_refs": refs},
                "after": {"associated_email_refs": sorted(after_refs)}}
    entity = session.get(Entity, handle.entity_id) if handle.entity_id else None
    before = {"entity_ref": entity.ref_id if entity else None,
              "status": handle.status, "binding_rule": handle.binding_rule}
    after = {"entity_ref": None, "binding_rule": None,
             "status": "retracted" if change.action == "retract" else "unbound"}
    if change.action == "bind":
        payload = change.payload.model_dump(mode="json", exclude_none=True)
        after.update({"entity_ref": payload.get("entity_ref"), "status": "bound",
                      "binding_rule": payload["resolution_basis"]["kind"]})
        if payload.get("entity_change_id"):
            after["entity_change_id"] = payload["entity_change_id"]
    result: dict[str, Any] = {"before": before, "after": after}
    if handle.handle_type == "sender_label" and change.action in {"retract", "unbind"}:
        refs = sorted(session.scalars(select(IdentityHandle.ref_id).join(
            SenderIdentityEmail,
            SenderIdentityEmail.email_identity_handle_id == IdentityHandle.id,
        ).where(
            SenderIdentityEmail.sender_identity_handle_id == handle.id,
            SenderIdentityEmail.status == "active",
        )))
        result["before"]["associated_email_refs"] = refs
        result["after"]["associated_email_refs"] = []
    return result


def _case_effect(session: Session, change: AttentionCaseResolutionInput) -> dict[str, Any]:
    header: dict[str, Any] = {
        "change_id": change.change_id, "mutation_type": change.mutation_type,
        "object_type": change.object_type, "action": change.action,
        "target_ref": change.object_ref, "case_revision_ref": change.case_revision_ref,
    }
    case = session.scalar(select(AttentionCase).where(AttentionCase.ref_id == change.object_ref))
    revision = session.scalar(select(AttentionCaseRevision).where(
        AttentionCaseRevision.ref_id == change.case_revision_ref,
    ))
    if case is None or revision is None or (
        revision.attention_case_id != case.id or revision.revision != case.latest_revision
        or case.status != "open"
    ):
        return {**header, "available": False, "reason": "case_revision_not_applicable"}
    items = {item.ref_id: item for item in session.scalars(select(CaseItem).where(
        CaseItem.attention_case_id == case.id,
    ))}
    selected = {item.case_item_ref: item.disposition for item in change.item_dispositions}
    if any(ref not in items or ref not in revision.case_item_refs or items[ref].status != "open"
           for ref in selected):
        return {**header, "available": False, "reason": "case_item_not_applicable"}
    remaining = [item for ref, item in items.items()
                 if ref not in selected and item.status == "open"]
    if change.case_outcome == "resolved" and any(
        item.resolution_role == "required" for item in remaining
    ):
        return {**header, "available": False, "reason": "required_case_items_unresolved"}
    derived = {item.ref_id: "not_pursued" for item in remaining
               if change.case_outcome != "keep_open"}
    dispositions = {**selected, **derived}
    return {
        **header, "available": True, "observed_version": case.version,
        "operator_disposition_refs": sorted(selected), "system_not_pursued_refs": sorted(derived),
        "before": {"status": case.status,
                   "item_statuses": {ref: items[ref].status for ref in sorted(dispositions)}},
        "after": {"status": (
                      case.status if change.case_outcome == "keep_open" else change.case_outcome),
                  "item_statuses": dict(sorted(dispositions.items()))},
    }


def capture_canonical_patch_preview(
    session: Session, content: ChangeSetContent | None,
) -> dict[str, Any]:
    if content is None:
        return {"format_version": 1, "available": False, "reason": "uncompiled_draft"}
    changes = [change for group in _GROUPS for change in getattr(content, group)
               if change.action != "create"]
    targets: dict[str, dict[str, Any]] = {}
    models: dict[str, type[Any]] = {kind: rule[0] for kind, rule in _RULES.items()}
    models.update({"identity_handle": IdentityHandle, "identity_binding": IdentityHandle})
    for kind, model in models.items():
        refs = {change.object_ref for change in changes
                if change.object_type == kind and change.object_ref is not None}
        if refs:
            targets[kind] = {row.ref_id: row for row in session.scalars(
                select(model).where(model.ref_id.in_(sorted(refs))),
            )}
    effects: list[dict[str, Any]] = []
    for change in changes:
        header: dict[str, Any] = {
            "change_id": change.change_id, "mutation_type": change.mutation_type,
            "object_type": change.object_type, "action": change.action,
            "target_ref": change.object_ref,
            "expected_version": content.expected_versions.get(change.object_ref),
        }
        row = targets.get(change.object_type, {}).get(change.object_ref)
        if row is None:
            if getattr(change, "object_change_id", None) is not None:
                header["target_change_id"] = change.object_change_id
            effects.append({**header, "available": False, "reason": (
                "same_changeset_target" if getattr(change, "object_change_id", None)
                else "canonical_target_unavailable"
            )})
            continue
        header.update({"available": True, "observed_version": row.version})
        if change.object_type in {"identity_handle", "identity_binding"}:
            effects.append({**header, **_identity_effect(session, change, row)})
            continue
        _model, patch_schema, status_field, retracted = _RULES[change.object_type]
        if change.action == "retract":
            patch = {status_field: retracted}
        elif change.action == "supersede" and change.object_type in _SUPERSEDING:
            patch = {status_field: "historical"}
            # Replacement data belongs to a newly created object, not this target.
            header["replacement_in_staged_input"] = True
        else:
            patch = change.payload.model_dump(mode="json", exclude_unset=True)
            patch = {key: value for key, value in patch.items()
                     if key in patch_schema.model_fields and key != "source_refs"}
            if change.object_type == "entity":
                # Registry updates ignore nulls, and trim display names.
                patch = {key: value for key, value in patch.items() if value is not None}
                if "display_name" in patch:
                    patch["display_name"] = patch["display_name"].strip()
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for key, value in patch.items():
            if key.endswith("_change_id"):
                if value is not None:
                    # No future public ref is invented for a same-draft dependency.
                    ref_field = key.removesuffix("_change_id") + "_ref"
                    before[ref_field] = _json_value(getattr(row, ref_field))
                    after[ref_field] = {"from_change_id": value}
                continue
            before[key] = _json_value(getattr(row, key))
            after[key] = value
        effects.append({**header, "before": before, "after": after})
    effects.extend(_case_effect(session, change) for change in content.resolution_changes)
    return {
        "format_version": 1, "available": True,
        "projection_kind": "canonical_target_patch", "effects": effects,
        "effect_count": len(effects), "sha256": sha256_json(effects),
    }

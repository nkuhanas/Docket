from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import CanonicalEvent, EventOccurrence
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventCreate,
    CanonicalEventModify,
    ChangeSetContent,
    EventChangeInput,
    LaneRoutingDecisionCreate,
)
from docket.schemas.event_occurrences import (
    CompiledOccurrenceEdit,
    EntireSeriesEventScope,
    OccurrenceEventScope,
    OccurrenceIdentity,
    OccurrenceReplacement,
)
from docket.services.event_occurrences import EventOccurrenceService


def compile_occurrence_changes(session: Session, content: ChangeSetContent) -> ChangeSetContent:
    if content.occurrence_plans:
        raise DocketError(
            code="occurrence_plans_compiler_owned", message="Occurrence plans are compiler-owned."
        )
    events: list[EventChangeInput] = []
    routes = list(content.lane_changes)
    plans: list[CompiledOccurrenceEdit] = []
    versions = dict(content.expected_versions)
    seen: set[str] = set()
    for change in content.event_changes:
        scope = getattr(change, "scope", None)
        if isinstance(scope, EntireSeriesEventScope) and change.action == "retract":
            # Explicit whole-series cancellation includes already moved children.
            # Their original coordinate and provenance remain in the ledger.
            for child_occurrence in session.scalars(
                select(EventOccurrence)
                .where(
                    EventOccurrence.series_ref == change.object_ref,
                    EventOccurrence.status == "replaced",
                )
                .order_by(EventOccurrence.original_start_key)
            ):
                identity = OccurrenceIdentity.model_validate(child_occurrence.identity_json)
                plan = EventOccurrenceService(session).plan(identity)
                child = session.scalar(
                    select(CanonicalEvent).where(
                        CanonicalEvent.ref_id == plan.replacement_event_ref
                    )
                )
                assert child is not None
                versions[child.ref_id] = child.version
                token = sha256_json(
                    {"source": change.change_id, "identity": identity.coordinate_key}
                )[:24]
                cancel = CanonicalEventCancel(
                    change_id=f"occ-{token}-replacement",
                    action="retract",
                    object_type="canonical_event",
                    object_ref=child.ref_id,
                    affected_fields=["status"],
                    basis_refs=change.basis_refs,
                )
                events.append(cancel)
                plans.append(
                    CompiledOccurrenceEdit(
                        source_change_id=change.change_id,
                        source_change=change.model_dump(mode="json", exclude_none=True),
                        source_scope=scope,
                        plan=plan,
                        replacement_change_id=cancel.change_id,
                        action_hashes={
                            cancel.change_id: sha256_json(
                                cancel.model_dump(mode="json", exclude_none=True)
                            )
                        },
                        basis_refs=change.basis_refs,
                    )
                )
        if not isinstance(scope, OccurrenceEventScope):
            events.append(change)
            continue
        if change.object_ref != scope.identity.series_ref:
            raise DocketError(
                code="occurrence_series_mismatch",
                message="Occurrence identity targets another series.",
            )
        if change.object_ref in seen:
            raise DocketError(
                code="overlapping_occurrence_edits",
                message="Stage one occurrence edit per series in a semantic request.",
            )
        seen.add(change.object_ref)
        service = EventOccurrenceService(session)
        plan = service.plan(scope.identity)
        series = session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == change.object_ref)
        )
        assert series is not None
        if versions.get(series.ref_id) != series.version:
            raise DocketError(
                code="version_conflict",
                message="Review the current series before staging its edit.",
            )
        if change.action != "retract":
            if not isinstance(change, CanonicalEventModify):
                raise DocketError(
                    code="occurrence_action_invalid", message="Unsupported occurrence action."
                )
            patch = change.payload.model_dump(mode="json", exclude_unset=True)
            if set(patch) - {"title", "event_spec"}:
                raise DocketError(
                    code="occurrence_patch_scope_invalid",
                    message=(
                        "Occurrence edits may change its title, time, location or notes, "
                        "not series context."
                    ),
                )
            child = (
                session.scalar(
                    select(CanonicalEvent).where(
                        CanonicalEvent.ref_id == plan.replacement_event_ref
                    )
                )
                if plan.replacement_event_ref
                else None
            )
            original = (
                child.event_spec
                if child is not None
                else {
                    **series.event_spec,
                    "timing": plan.original_timing,
                    "recurrence": None,
                }
            )
            replacement_spec = patch.get("event_spec") or original
            if replacement_spec.get("recurrence") or (
                replacement_spec.get("calendar_lane") != series.event_spec.get("calendar_lane")
            ):
                raise DocketError(
                    code="occurrence_patch_scope_invalid",
                    message="An occurrence edit cannot change recurrence or destination.",
                )
            replacement = OccurrenceReplacement.model_validate(
                {key: replacement_spec.get(key) for key in ("title", "timing", "location", "notes")}
            )
            if patch.get("title"):
                replacement = replacement.model_copy(update={"title": patch["title"]})
            plan = service.plan(scope.identity, replacement)
        generated: list[EventChangeInput] = []
        child_change_id: str | None = None
        if not plan.no_op:
            # Keep a no-content-change master update when only a moved child is
            # changing: it remains the optimistic concurrency lock for this identity.
            master = CanonicalEventModify(
                change_id=change.change_id,
                action="update",
                object_type="canonical_event",
                object_ref=series.ref_id,
                scope=scope,
                payload={"event_spec": plan.master_after},
                affected_fields=["recurrence.exceptions"],
                basis_refs=change.basis_refs,
            )
            generated.append(master)
            token = sha256_json({"source_change_id": change.change_id})[:24]
            child_change_id = f"occ-{token}-replacement"
            if plan.replacement_event_ref is not None:
                child = session.scalar(
                    select(CanonicalEvent).where(
                        CanonicalEvent.ref_id == plan.replacement_event_ref
                    )
                )
                assert child is not None
                versions[child.ref_id] = child.version
                if plan.status == "cancelled":
                    generated.append(
                        CanonicalEventCancel(
                            change_id=child_change_id,
                            action="retract",
                            object_type="canonical_event",
                            object_ref=child.ref_id,
                            affected_fields=["status"],
                            basis_refs=change.basis_refs,
                        )
                    )
                else:
                    assert plan.replacement_after is not None
                    generated.append(
                        CanonicalEventModify(
                            change_id=child_change_id,
                            action="update",
                            object_type="canonical_event",
                            object_ref=child.ref_id,
                            payload={
                                "title": plan.replacement_after["title"],
                                "event_spec": plan.replacement_after,
                            },
                            affected_fields=["event"],
                            basis_refs=change.basis_refs,
                        )
                    )
            elif plan.replacement_after is not None:
                generated.append(
                    CanonicalEventCreate(
                        change_id=child_change_id,
                        action="create",
                        object_type="canonical_event",
                        create_spec={
                            "canonical_key": (
                                f"occurrence:{series.ref_id}:{scope.identity.coordinate_key}"
                            ),
                            "title": plan.replacement_after["title"],
                            "event_spec": plan.replacement_after,
                            "lane_ref": series.lane_ref,
                            "entity_refs": series.entity_refs,
                        },
                        affected_fields=["event"],
                        basis_refs=change.basis_refs,
                    )
                )
                routes.append(
                    LaneRoutingDecisionCreate(
                        change_id=f"occ-{token}-route",
                        action="create",
                        object_type="lane_routing_decision",
                        create_spec={
                            "event_change_id": child_change_id,
                            "lane_ref": series.lane_ref,
                            "decision_kind": "explicit_operator",
                            "operator_confirmed": True,
                        },
                        affected_fields=["lane"],
                        basis_refs=change.basis_refs,
                    )
                )
            else:
                child_change_id = None
        plans.append(
            CompiledOccurrenceEdit(
                source_change_id=change.change_id,
                source_change=change.model_dump(mode="json", exclude_none=True),
                source_scope=scope,
                plan=plan,
                replacement_change_id=child_change_id,
                action_hashes={
                    item.change_id: sha256_json(item.model_dump(mode="json", exclude_none=True))
                    for item in generated
                },
                basis_refs=change.basis_refs,
            )
        )
        events.extend(generated)
    return ChangeSetContent.model_validate(
        {
            **content.model_dump(mode="json"),
            "event_changes": events,
            "lane_changes": routes,
            "expected_versions": versions,
            "occurrence_plans": plans,
        }
    )

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import CanonicalEvent, EventOccurrence
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventModify,
    ChangeSetContent,
    InternalEventChange,
    LaneRoutingDecisionCreate,
    MaterializedEventCreate,
    MaterializedEventModify,
    mutation_input_json,
)
from docket.schemas.event_occurrences import (
    CompiledOccurrenceEdit,
    EntireSeriesEventScope,
    OccurrenceEventScope,
    OccurrenceIdentity,
    OccurrenceReplacement,
)
from docket.services.event_field_patches import materialize_event_patch, merge_fields, patch_error
from docket.services.event_occurrences import EventOccurrenceService


def compile_occurrence_changes(session: Session, content: ChangeSetContent) -> ChangeSetContent:
    if content.occurrence_plans:
        raise DocketError(
            code="occurrence_plans_compiler_owned", message="Occurrence plans are compiler-owned."
        )
    events: list[InternalEventChange] = []
    routes = list(content.lane_changes)
    plans: list[CompiledOccurrenceEdit] = []
    versions = dict(content.expected_versions)
    seen: set[str] = set()
    for change in content.event_changes:
        if isinstance(change, MaterializedEventModify):
            raise DocketError(code="event_materialization_compiler_owned",
                              message="Stage a sparse event patch, not executable records.")
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
                        source_change=mutation_input_json(change),
                        source_scope=scope,
                        plan=plan,
                        replacement_change_id=cancel.change_id,
                        action_hashes={
                            cancel.change_id: sha256_json(
                                mutation_input_json(cancel)
                            )
                        },
                        basis_refs=change.basis_refs,
                    )
                )
        if not isinstance(scope, OccurrenceEventScope):
            events.append(materialize_event_patch(session, change, versions)
                          if isinstance(change, CanonicalEventModify) else change)
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
        try:
            plan = service.plan(scope.identity)
        except DocketError as exc:
            raise patch_error(change, exc.code, ["scope", "identity"],
                              "read_current_occurrence") from exc
        series = session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == change.object_ref)
        )
        assert series is not None
        if versions.get(series.ref_id) != series.version:
            raise patch_error(change, "version_conflict", ["expected_versions", series.ref_id],
                              "reconcile_event_version")
        if change.action != "retract":
            if not isinstance(change, CanonicalEventModify):
                raise DocketError(
                    code="occurrence_action_invalid", message="Unsupported occurrence action."
                )
            patch = change.payload.model_dump(mode="json", exclude_unset=True)
            unsupported = sorted(set(patch) - {"event_spec"})
            if unsupported:
                raise patch_error(change, "occurrence_patch_scope_invalid",
                                  ["payload", unsupported[0]], "stage_occurrence_fields_only")
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
            if child is not None:
                if child.ref_id in versions and versions[child.ref_id] != child.version:
                    raise patch_error(
                        change, "version_conflict", ["expected_versions", child.ref_id],
                        "reconcile_event_version",
                    )
                versions[child.ref_id] = child.version
            assert change.payload.event_spec is not None
            replacement_spec = merge_fields(original, change.payload.event_spec)
            replacement = OccurrenceReplacement.model_validate(
                {key: replacement_spec.get(key) for key in ("title", "timing", "location", "notes")}
            )
            try:
                plan = service.plan(scope.identity, replacement)
            except DocketError as exc:
                details = exc.details or {}
                raise patch_error(
                    change, exc.code, details.get("field_path", ["scope", "identity"]),
                    details.get("next_action", "read_current_occurrence"),
                ) from exc
        generated: list[InternalEventChange] = []
        child_change_id: str | None = None
        if not plan.no_op:
            # Keep a no-content-change master update when only a moved child is
            # changing: it remains the optimistic concurrency lock for this identity.
            # Cancellation also produces a compiler-owned master exception patch.
            source_patch = change if isinstance(change, CanonicalEventModify) else (
                CanonicalEventModify(
                    change_id=change.change_id, action="update", object_type="canonical_event",
                    object_ref=series.ref_id, scope=scope,
                    payload={"recurrence": plan.master_after["recurrence"]},
                    affected_fields=["event_spec.recurrence"], basis_refs=change.basis_refs,
                )
            )
            master = MaterializedEventModify(
                change_id=change.change_id,
                action="update",
                object_type="canonical_event",
                object_ref=series.ref_id,
                scope=scope,
                payload={"event_spec": plan.master_after},
                source_patch=source_patch,
                no_op=series.event_spec == plan.master_after,
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
                        MaterializedEventModify(
                            change_id=child_change_id,
                            action="update",
                            object_type="canonical_event",
                            object_ref=child.ref_id,
                            payload={
                                "title": plan.replacement_after["title"],
                                "event_spec": plan.replacement_after,
                            },
                            source_patch=source_patch,
                            affected_fields=["event"],
                            basis_refs=change.basis_refs,
                        )
                    )
            elif plan.replacement_after is not None:
                generated.append(
                    MaterializedEventCreate(
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
                source_change=mutation_input_json(change),
                source_scope=scope,
                plan=plan,
                replacement_change_id=child_change_id,
                action_hashes={
                    item.change_id: sha256_json(mutation_input_json(item))
                    for item in generated
                },
                basis_refs=change.basis_refs,
            )
        )
        events.extend(generated)
    return ChangeSetContent.model_validate(
        {
            **mutation_input_json(content, exclude_none=False),
            "event_changes": events,
            "lane_changes": routes,
            "expected_versions": versions,
            "occurrence_plans": plans,
        }
    )

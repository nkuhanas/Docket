"""Bind fallible source interpretations, then enforce exact mechanical repairs.

The authenticated instruction is the authority. A complete selected-entry
inventory is bound once; initial values arrive in bounded staging batches.
Neither this record nor its digest asserts independent OCR/semantic truth.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttachmentEvidence,
    ChangeSet,
    OperatorUtterance,
    RequestEntryInterpretation,
    SemanticRequest,
    Source,
)
from docket.schemas.assembly import (
    AssemblyAuthorityScopeInput,
    NormalizedEntryInput,
    ScheduledOccurrenceEntry,
)
from docket.schemas.authority import (
    CanonicalEventCreate,
    ChangeSetContent,
    ItemCreate,
    TemporalBindingCreate,
)
from docket.schemas.request_specifications import RequestEntryInterpretationInput
from docket.services.changeset_compiler import entry_facets

_ENTRY: TypeAdapter[NormalizedEntryInput] = TypeAdapter(NormalizedEntryInput)
_METADATA = {
    "statement_ref", "compiler_identifier", "compiler_version", "input_schema_version",
    "normalized_input_hash", "compilation_errors",
}


def _conflict(entry_id: str, constraint: str, field_path: list[str]) -> DocketError:
    return DocketError(
        code="request_interpretation_conflict",
        message="This patch changes the selected source interpretation, not its compilation.",
        details={
            "category": "semantic_conflict", "entry_id": entry_id,
            "constraint": constraint, "field_path": field_path,
            "authority_preserved": True, "next_action": "reconcile_source_interpretation",
        },
    )


def _evidence_error(entry_id: str, constraint: str) -> DocketError:
    return DocketError(
        code="request_interpretation_evidence_invalid",
        message="The original interpretation's evidence binding cannot be verified.",
        details={
            "category": "evidence_validation", "entry_id": entry_id,
            "constraint": constraint, "field_path": ["evidence"],
            "authority_preserved": True, "next_action": "restore_original_source_evidence",
        },
    )


def _source_binding(session: Session, entry: NormalizedEntryInput) -> dict[str, Any]:
    source = session.scalar(select(Source).where(Source.ref_id == entry.evidence.source_ref))
    attachment = session.scalar(select(AttachmentEvidence).where(
        AttachmentEvidence.ref_id == entry.evidence.source_ref,
    ))
    if source is None or (attachment is not None and attachment.ingest_state != "available"):
        raise _evidence_error(entry.import_entry_id, "available_bound_source")
    return {
        "source_ref": source.ref_id, "source_manifest_hash": source.content_hash,
        "attachment_content_hash": attachment.content_hash if attachment is not None else None,
        "evidence_state": "attachment_recorded" if attachment is not None else "source_recorded",
    }


def _entry_value(entry: NormalizedEntryInput) -> dict[str, Any]:
    # Normalize only typed entry defaults. No recursive stripping of opaque
    # content, source coordinates, destination, original date or semantic values.
    value = entry.model_dump(mode="json", exclude_none=True)
    # Correcting a fragment checksum is provenance completion, not a new title,
    # date or destination. The declared extractor still independently verifies
    # the checksum before commitment; its source and locator stay fixed.
    value["evidence"].pop("source_fragment_hash", None)
    return value


def read_entry_interpretation(
    session: Session, *, request_ref: str, entry_id: str,
) -> RequestEntryInterpretationInput:
    row = session.get(RequestEntryInterpretation, (request_ref, entry_id))
    if row is None:
        raise DocketError(
            code="request_interpretation_migration_required",
            message="The preserved entry has no separately recorded initial interpretation.",
            details={
                "entry_id": entry_id, "authority_preserved": True,
                "next_action": "reconcile_preserved_request", "historical_backfill": False,
            },
        )
    try:
        interpreted = RequestEntryInterpretationInput.model_validate(row.interpretation_json)
    except ValidationError as exc:
        raise _evidence_error(entry_id, "supported_interpretation_schema") from exc
    if row.interpretation_hash != sha256_json(interpreted.model_dump(mode="json")) or (
        interpreted.entry.import_entry_id != entry_id
    ):
        raise _evidence_error(entry_id, "immutable_interpretation_digest")
    origins = {
        origin.utterance_ref: origin.content_hash for origin in interpreted.originating_utterances
    }
    actual = {origin.ref_id: origin.content_hash for origin in session.scalars(
        select(OperatorUtterance).where(OperatorUtterance.ref_id.in_(origins))
    )}
    if actual != origins:
        raise _evidence_error(entry_id, "unchanged_original_utterance")
    if _source_binding(session, interpreted.entry) != interpreted.source_binding.model_dump(
        mode="json",
    ):
        raise _evidence_error(entry_id, "unchanged_original_source")
    return interpreted


def bind_entry_interpretation(
    session: Session, *, request: SemanticRequest, scope: AssemblyAuthorityScopeInput,
    entry: NormalizedEntryInput, existing_draft_entry: bool,
) -> None:
    """Called under the assembly request lock, before editing or compiling a patch."""
    if not scope.selected_entry_ids:
        raise DocketError(
            code="source_selection_required",
            message="A source import needs its complete selected_entry_ids in the initial scope.",
            details={
                "field_path": ["assembly_scope", "selected_entry_ids"],
                "authority_preserved": True, "next_action": "supply_complete_source_selection",
            },
        )
    if entry.import_entry_id not in scope.selected_entry_ids:
        raise _conflict(entry.import_entry_id, "entry_in_original_selection", ["import_entry_id"])
    if entry.evidence.source_ref not in scope.source_refs:
        raise _conflict(
            entry.import_entry_id, "source_in_original_selection", ["evidence", "source_ref"],
        )
    row = session.get(RequestEntryInterpretation, (request.ref_id, entry.import_entry_id))
    if row is not None or existing_draft_entry:
        original = read_entry_interpretation(
            session, request_ref=request.ref_id, entry_id=entry.import_entry_id,
        )
        before, after = _entry_value(original.entry), _entry_value(entry)
        if before != after:
            changed = sorted(
                key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
            )
            raise _conflict(entry.import_entry_id, "unchanged_selected_entry", changed[:1])
        return
    origins = list(session.scalars(select(OperatorUtterance).where(
        OperatorUtterance.ref_id.in_(request.origin_utterance_refs),
    )))
    if {row.ref_id for row in origins} != set(request.origin_utterance_refs):
        raise _evidence_error(entry.import_entry_id, "original_utterance_present")
    interpreted = RequestEntryInterpretationInput.model_validate({
        "originating_utterances": [
            {"utterance_ref": row.ref_id, "content_hash": row.content_hash}
            for row in sorted(origins, key=lambda row: row.ref_id)
        ],
        "source_binding": _source_binding(session, entry), "entry": entry,
    })
    payload = interpreted.model_dump(mode="json")
    session.add(RequestEntryInterpretation(
        semantic_request_ref=request.ref_id, entry_id=entry.import_entry_id,
        interpretation_json=payload, interpretation_hash=sha256_json(payload),
    ))


def selection_status(
    session: Session, *, request_ref: str, scope: AssemblyAuthorityScopeInput,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify the exact selected entry set; an incomplete batch is saved, not committed."""
    selected = set(scope.selected_entry_ids)
    if not selected and not entries:
        return {}
    current = {str(entry["import_entry_id"]): entry for entry in entries}
    if not selected or current.keys() - selected:
        raise _conflict("request", "complete_original_entry_inventory", ["selected_entry_ids"])
    bindings = []
    for entry_id, record in sorted(current.items()):
        original = read_entry_interpretation(session, request_ref=request_ref, entry_id=entry_id)
        try:
            parsed = _ENTRY.validate_python({k: v for k, v in record.items() if k not in _METADATA})
        except ValidationError as exc:
            raise _evidence_error(entry_id, "supported_normalized_entry") from exc
        if _entry_value(parsed) != _entry_value(original.entry):
            raise _conflict(entry_id, "unchanged_selected_entry", ["entry"])
        bindings.append({"entry_id": entry_id, "interpretation_hash": sha256_json(
            original.model_dump(mode="json"),
        )})
    missing = sorted(selected - current.keys())
    return {
        "interpretation_state": "recorded_interpretation",
        "selected_entry_count": len(selected), "staged_entry_count": len(current),
        "missing_entry_count": len(missing), "missing_entry_ids": missing[:3],
        "complete": not missing,
        # Completeness fixes the hash forever: no later entries or substituted
        # values can satisfy the immutable selection plus append-only entry rows.
        "interpretation_hash": sha256_json({
            "selected_entry_ids": sorted(selected), "entries": bindings,
            "exclusions": scope.explicit_exclusions,
        }) if not missing else None,
    }


def verify_draft_interpretation(session: Session, changeset: ChangeSet) -> None:
    if not changeset.normalized_entries_json:
        return
    request = session.scalar(select(SemanticRequest).where(
        SemanticRequest.ref_id == changeset.semantic_request_ref,
    ))
    if request is None:
        raise _evidence_error("request", "original_request_present")
    scope = AssemblyAuthorityScopeInput.model_validate(
        (request.selected_option_binding or {}).get("scope"),
    )
    status = selection_status(
        session, request_ref=request.ref_id, scope=scope,
        entries=changeset.normalized_entries_json,
    )
    if changeset.compiler_manifest_json.get("source_interpretation") != status:
        raise _evidence_error("request", "exact_revision_interpretation_binding")


def compiled_interpretation_errors(
    session: Session, *, request_ref: str | None, content: ChangeSetContent,
) -> list[dict[str, Any]]:
    """Check actual compiled effects, not just the entry or a compiler self-report."""
    request = session.scalar(select(SemanticRequest).where(SemanticRequest.ref_id == request_ref))
    if request is None or (
        (request.selected_option_binding or {}).get("kind") != "freeform_assembly"
    ):
        return []
    scope = AssemblyAuthorityScopeInput.model_validate(
        (request.selected_option_binding or {})["scope"],
    )
    if not scope.selected_entry_ids:
        return []
    errors: list[dict[str, Any]] = []

    def invalid(entry_id: str, field: str, constraint: str) -> None:
        errors.append({
            "code": "source_interpretation_compilation_mismatch",
            "category": "implementation_validation", "entry_id": entry_id,
            "field_path": [field], "constraint": constraint,
            "next_action": "repair_compilation_preserving_interpretation",
        })

    coverage = {row.entry_id: row for row in content.import_scope.entry_coverage} if (
        content.import_scope is not None
    ) else {}
    if set(coverage) != set(scope.selected_entry_ids):
        invalid("request", "entry_coverage", "exact_selected_entry_set")
        return errors
    items = {row.change_id: row for row in content.tracked_context_changes
             if isinstance(row, ItemCreate)}
    times = {row.change_id: row for row in content.tracked_context_changes
             if isinstance(row, TemporalBindingCreate)}
    events = {row.change_id: row for row in content.event_changes
              if isinstance(row, CanonicalEventCreate)}
    covered_events: set[str] = set()
    for entry_id, covered in coverage.items():
        entry = read_entry_interpretation(
            session, request_ref=request.ref_id, entry_id=entry_id,
        ).entry
        item = items.get(covered.item_change_id)
        time = times.get(covered.temporal_binding_change_id)
        item_input, temporal_input = entry_facets(entry)
        expected_item = item_input.model_copy(update={"source_refs": [entry.evidence.source_ref]})
        if item is None or item.create_spec != expected_item:
            invalid(entry_id, "item", "item_matches_initial_interpretation")
        if time is None or (
            time.create_spec.subject_change_id != covered.item_change_id
            or time.create_spec.role != temporal_input.role
            or time.create_spec.binding_key != temporal_input.binding_key
            or time.create_spec.temporal_value != temporal_input.temporal_value
        ):
            invalid(entry_id, "temporal", "time_matches_initial_interpretation")
        event = events.get(covered.calendar_change_id or "")
        if not isinstance(entry, ScheduledOccurrenceEntry):
            if covered.calendar_change_id is not None or event is not None:
                invalid(entry_id, "calendar", "no_occurrence_in_selected_entry")
            continue
        if event is None:
            invalid(entry_id, "calendar", "one_event_for_selected_occurrence")
            continue
        covered_events.add(event.change_id)
        spec = event.create_spec
        expected = {
            "title": entry.title, "timing": entry.timing, "location": entry.location,
            "notes": entry.description, "recurrence": None,
        }
        for field, value in expected.items():
            if getattr(spec.event_spec, field) != value:
                invalid(entry_id, field, "event_matches_initial_interpretation")
        if spec.title != entry.title:
            invalid(entry_id, "title", "canonical_title_matches_initial_interpretation")
        if spec.lane_ref != entry.lane_ref or spec.lane_change_id != entry.lane_change_id:
            invalid(entry_id, "lane", "destination_matches_initial_interpretation")
        if spec.entity_refs != entry.context_entity_refs:
            invalid(entry_id, "entity_refs", "context_matches_initial_interpretation")
    if covered_events != set(events):
        invalid("request", "event_changes", "no_unselected_created_occurrences")
    return errors

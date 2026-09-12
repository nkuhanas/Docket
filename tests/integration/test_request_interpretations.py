"""Synthetic interpretations test the trust boundary, not model vision accuracy."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from test_changeset_assembly import _admit, _schedule_context, _schedule_stage, _utterance
from test_request_specifications import _stage
from test_source_title_repair import _fixture

from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CanonicalEvent,
    ChangeSetRevision,
    Operation,
    RequestEntryInterpretation,
    SemanticRequest,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.request_interpretations import read_entry_interpretation
from docket.services.request_specifications import read_request_proposal


@pytest.mark.parametrize("attack", ["fourth_entry", "date", "destination", "title"])
def test_repair_patch_cannot_change_initial_image_interpretation(session, monkeypatch, attack):
    utterance, service, draft, lanes, admit, _ = _fixture(session, monkeypatch, image=True)
    request = session.scalar(select(SemanticRequest))
    original_hash = request.authority_scope_hash
    original_draft = deepcopy(draft.normalized_entries_json)
    original = read_request_proposal(session, semantic_request_ref=request.ref_id, version=1)
    operations = [{"operation": "normalized_entry_upsert", "entry": entry.model_dump(mode="json")}
                  for entry in original.normalized_entries]
    corrected = deepcopy(operations)
    if attack == "fourth_entry":
        fourth = deepcopy(operations[-1])
        fourth["entry"]["import_entry_id"] = "fair-four"
        operations.append(fourth)
    elif attack == "date":
        operations[0]["entry"]["timing"].update({
            "start_local": "2026-09-20T10:00:00", "end_local": "2026-09-20T15:00:00",
        })
    elif attack == "destination":
        operations[0]["entry"]["lane_ref"] = lanes[1].ref_id
    else:
        operations[0]["entry"]["title"] = "A different event"
    payload = StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "patch": {"operations": operations},
    })
    token = admit(3, "docket_stage_changes")
    # Same transaction/savepoint contract as the authenticated MCP wrapper.
    with pytest.raises(DocketError) as error, session.begin_nested():
        service.stage(payload, assembly_operation_token=token, assembly_argument_hash="3" * 64)
    assert error.value.code == "request_interpretation_conflict"
    rejected = service.reject_admitted_operation(
        token=token, argument_hash="3" * 64, operation_kind="stage",
        utterance_ref=utterance.ref_id, error=error.value,
    )
    assert rejected["disposition"] == "rejected_conflict"
    assert rejected["error"]["details"]["next_action"] == "reconcile_source_interpretation"
    assert draft.normalized_entries_json == original_draft
    assert draft.current_revision == 1
    assert session.scalar(select(func.count(RequestEntryInterpretation.entry_id))) == 3
    assert request.authority_availability == "available"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0

    # Correcting the compiler without changing the three selected inputs is a
    # new operation in the same request; it can stage + commit without review.
    repaired = service.stage(StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "patch": {"operations": corrected},
    }), assembly_operation_token=admit(4, "docket_stage_changes"), assembly_argument_hash="4" * 64)
    assert repaired["disposition"] == "ready_to_commit"
    assert repaired["source_interpretation"]["complete"] is True
    assert repaired["source_interpretation"]["selected_entry_count"] == 3
    receipt = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=admit(5, "docket_commit_changeset"),
        assembly_argument_hash="5" * 64,
    )
    assert receipt["disposition"] == "committed"
    assert request.authority_scope_hash == original_hash
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 3
    assert session.scalar(select(func.count(Operation.id))) == 3
    assert service.stage(
        payload, assembly_operation_token=token, assembly_argument_hash="3" * 64,
    ) == {
        **rejected, "replayed": True,
    }
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 2


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_initial_interpretation_is_append_only(session, monkeypatch, mutation):
    _, _, draft, _, _, _ = _fixture(session, monkeypatch, image=True)
    session.commit()
    row = session.scalar(select(RequestEntryInterpretation))
    interpreted = read_entry_interpretation(
        session, request_ref=draft.semantic_request_ref, entry_id=row.entry_id,
    )
    assert interpreted.interpretation_state == "recorded_interpretation"
    if mutation == "update":
        row.interpretation_json = {**row.interpretation_json, "interpretation_state": "verified"}
    else:
        session.delete(row)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()


def test_repair_cannot_rebind_changed_source_or_corrupt_baseline(session, monkeypatch):
    _, _, draft, _, _, _ = _fixture(session, monkeypatch, image=True)
    row = session.scalar(select(RequestEntryInterpretation))
    key = (draft.semantic_request_ref, row.entry_id)
    session.execute(RequestEntryInterpretation.__table__.update().values(
        interpretation_hash="0" * 64,
    ))  # SQLite bypass only; PostgreSQL trigger rejection is rehearsed separately.
    session.expire_all()
    with pytest.raises(DocketError) as error:
        read_entry_interpretation(session, request_ref=key[0], entry_id=key[1])
    assert error.value.code == "request_interpretation_evidence_invalid"
    assert error.value.details["constraint"] == "immutable_interpretation_digest"


@pytest.mark.parametrize("field", ["title", "timing"])
def test_consistently_wrong_compiler_copies_still_cannot_commit(session, monkeypatch, field):
    import docket.services.changeset_assembly as assembly

    utterance, service, draft, _, admit, compiler = _fixture(session, monkeypatch, image=True)
    proposal = read_request_proposal(session, semantic_request_ref=draft.semantic_request_ref,
                                     version=1)

    def wrong_compiler(*args, **kwargs):
        compiled = compiler(*args, **kwargs)
        actions = deepcopy(list(compiled.actions))
        for action in actions:
            spec = action["create_spec"]
            if field == "title":
                if action["mutation_type"] in {"item_create", "canonical_event_create"}:
                    spec["title"] = "Consistently wrong title"
                if action["mutation_type"] == "canonical_event_create":
                    spec["event_spec"]["title"] = "Consistently wrong title"
            elif action["mutation_type"] in {"canonical_event_create", "temporal_binding_create"}:
                timing = spec["event_spec"]["timing"] if (
                    action["mutation_type"] == "canonical_event_create"
                ) else spec["temporal_value"]
                timing["start_local"] = "2026-09-20T10:00:00"
                timing["end_local"] = "2026-09-20T15:00:00"
        return replace(compiled, actions=tuple(actions))

    with monkeypatch.context() as patch:
        patch.setattr(assembly, "compile_normalized_entry", wrong_compiler)
        staged = service.stage(StageChangesInput.model_validate({
            "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
            "patch": {"operations": [{"operation": "normalized_entry_upsert",
                                      "entry": entry.model_dump(mode="json")}
                                     for entry in proposal.normalized_entries]},
        }), assembly_operation_token=admit(3, "docket_stage_changes"),
            assembly_argument_hash="3" * 64)
    assert staged["disposition"] == "saved_with_errors"
    assert any(error["code"] == "source_interpretation_compilation_mismatch"
               for error in draft.validation_errors)
    assert not any(error["code"] == "import_entry_calendar_title_mismatch"
                   for error in draft.validation_errors)
    rejected = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=admit(4, "docket_commit_changeset"),
        assembly_argument_hash="4" * 64,
    )
    assert rejected["disposition"] == "rejected_validation"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0


def test_partial_selection_review_keeps_unstaged_ids_at_its_original_revision(session):
    utterance = _utterance("1542799000000000950")
    source, lane = _schedule_context(session, utterance, suffix="partial-selection")
    trace = new_public_ref("trace")
    first = _stage(session, utterance, trace, 1, _schedule_stage(
        utterance, source_ref=source.ref_id, lane_ref=lane.ref_id, start_index=0,
        count=2, include_scope=True, selected_count=4,
    ))
    assert first["disposition"] == "saved_with_errors"
    assert first["source_interpretation"]["missing_entry_count"] == 2
    service = ChangeSetAssemblyService(session)

    def review(ordinal, cursor):
        token = _admit(
            session, utterance=utterance, trace_ref=trace, call_id=f"selection-review-{ordinal}",
            ordinal=ordinal, tool_name="docket_review_changeset", argument_hash=str(ordinal) * 64,
        )
        return service.review(ReviewChangesInput(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            view="entries", cursor=cursor, limit=1,
        ), assembly_operation_token=token, assembly_argument_hash=str(ordinal) * 64)

    page = review(2, None)
    assert page["total_if_known"] == 4
    rows, cursor = list(page["items"]), page["cursor"]
    completed = _stage(session, utterance, trace, 3, _schedule_stage(
        utterance, source_ref=source.ref_id, lane_ref=lane.ref_id, start_index=2,
        count=2, include_scope=False,
    ))
    assert completed["disposition"] == "ready_to_commit"
    assert completed["source_interpretation"]["complete"] is True
    ordinal = 4
    while cursor:
        page = review(ordinal, cursor)
        assert page["revision"] == 1 and page["is_current_revision"] is False
        assert page["source_interpretation"]["missing_entry_count"] == 2
        rows.extend(page["items"])
        cursor = page.get("cursor")
        ordinal += 1
    missing = [row for row in rows if row.get("staging_state") == "not_staged"]
    assert {row["import_entry_id"] for row in missing} == {
        "math-1263-entry-02", "math-1263-entry-03",
    }
    assert all("title" not in row and not row["entry_type_known"] for row in missing)

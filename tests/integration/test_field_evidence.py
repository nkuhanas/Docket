"""Mixed-source contracts, not an evaluation of native vision accuracy."""

import base64
import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select
from test_attachment_evidence import _request
from test_changeset_assembly import _admit

from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    Item,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    RequestFieldEvidence,
    SemanticRequest,
    TemporalBinding,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.authority import ChangeSetContent
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.field_evidence import read_proof
from docket.services.provenance import ProvenanceService

BUILDING = "Warren J. Baker Center for Science and Mathematics"
DATES = ["09-17", "09-24", "10-08", "10-22", "11-05", "11-19", "12-03"]
ROOMS = ["102", "113", "102", "102", "113", "102", "102"]


def _fixture(session):
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="
    )
    capture = ProvenanceService(session).capture_operator_utterance(_request(
        message_id="1542999000000000771", content=content,
        filename="building.png", media_type="image/png",
    ).model_copy(update={"verbatim_text": (
        "Add seven CSAI meetings: 9/17 7:50pm 102; 9/24 8pm 113; 10/8 7:30pm 102; "
        "10/22 7:30pm 102; 11/5 7:30pm 113; 11/19 7:30pm 102; 12/3 7:30pm 102. "
        "Use the attached building name. Use one hour for each meeting."
    )}))
    utterance = session.scalar(select(OperatorUtterance).where(
        OperatorUtterance.ref_id == capture["ref"],
    ))
    source = capture["attachments"][0]["ref"]
    account = ProviderAccount(
        provider="google", external_account_id="mixed-evidence-smoke",
        capabilities=["google_calendar"], enabled=True,
    )
    session.add(account)
    session.flush()
    lane = CalendarLane(
        account_id=account.id, lane="csai", display_name="CSAI", color_hex="#3367D6",
        calendar_id="csai@example.com", status="active", basis_refs=[utterance.ref_id],
        created_by_changeset_ref="chg_01M1A100000000000000000000",
    )
    session.add(lane)
    session.flush()
    actions = []
    for index, (day, room) in enumerate(zip(DATES, ROOMS, strict=True)):
        start, end = ("19:50", "20:50") if index == 0 else (
            ("20:00", "21:00") if index == 1 else ("19:30", "20:30")
        )
        actions.extend([{
            "mutation_type": "canonical_event_create", "action": "create",
            "object_type": "canonical_event", "change_id": f"meeting-{index}",
            "affected_fields": ["event_spec"], "basis_refs": [utterance.ref_id, source],
            "create_spec": {"title": "CSAI Meeting", "lane_ref": lane.ref_id, "event_spec": {
                "calendar_lane": lane.lane,
                "title": "CSAI Meeting", "location": f"{BUILDING}, Room {room}",
                "timing": {"kind": "timed", "start_local": f"2026-{day}T{start}:00",
                           "end_local": f"2026-{day}T{end}:00", "timezone": "America/Los_Angeles"},
            }},
        }, {
            "mutation_type": "lane_routing_decision_create", "action": "create",
            "object_type": "lane_routing_decision", "change_id": f"route-{index}",
            "affected_fields": ["routing"], "basis_refs": [utterance.ref_id],
            "create_spec": {"event_change_id": f"meeting-{index}",
                            "lane_ref": lane.ref_id, "decision_kind": "explicit_operator",
                            "operator_confirmed": True},
        }])
    binding = {
        "operation": "field_evidence_bind", "bindings": [{
            "source_ref": source, "source_fragment_locator": {"region": "building_name"},
            "extractor_identifier": "hermes.native-vision", "extractor_version": "fixture-v1",
            "value": BUILDING,
            "targets": [{"change_id": f"meeting-{i}",
                         "field_path": "create_spec.event_spec.location", "match": "prefix"}
                        for i in range(7)],
        }],
    }
    scope = {
        "resolved_intent": {"intent": "add seven meetings", "duration_minutes": 60},
        "allowed_mutation_types": ["canonical_event_create", "lane_routing_decision_create"],
        "target_refs": [lane.ref_id], "source_refs": [source],
    }
    trace = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)

    def admit(ordinal, tool):
        return _admit(session, utterance=utterance, trace_ref=trace,
                      call_id=f"fields-{ordinal}", ordinal=ordinal, tool_name=tool,
                      argument_hash=f"{ordinal:064x}")

    def stage(ordinal, operations, *, initial=False, scope_update=None):
        payload = StageChangesInput.model_validate({
            "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
            **({"assembly_scope": {**scope, **(scope_update or {})}} if initial else {}),
            "patch": {"operations": operations},
        })
        return service.stage(
            payload, assembly_operation_token=admit(ordinal, "docket_stage_changes"),
            assembly_argument_hash=f"{ordinal:064x}",
        )

    def commit(ordinal):
        return service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            assembly_operation_token=admit(ordinal, "docket_commit_changeset"),
            assembly_argument_hash=f"{ordinal:064x}",
        )

    def review(ordinal):
        return service.review(
            ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
            assembly_operation_token=admit(ordinal, "docket_review_changeset"),
            assembly_argument_hash=f"{ordinal:064x}",
        )

    operations = [{"operation": "action_upsert", "action": action} for action in actions]
    return utterance, service, lane, operations, binding, stage, commit, review


def _assert_committed(session, lane):
    events = sorted(session.scalars(select(CanonicalEvent)),
                    key=lambda row: row.event_spec["timing"]["start_local"])
    assert len(events) == 7
    assert all(row.title == "CSAI Meeting" and row.lane_ref == lane.ref_id for row in events)
    assert [row.event_spec["location"] for row in events] == [
        f"{BUILDING}, Room {room}" for room in ROOMS
    ]
    assert [row.event_spec["timing"]["start_local"] for row in events] == [
        f"2026-{day}T{time}:00" for day, time in zip(
            DATES, ["19:50", "20:00", *(["19:30"] * 5)], strict=True,
        )
    ]
    assert [row.event_spec["timing"]["end_local"] for row in events] == [
        f"2026-{day}T{time}:00" for day, time in zip(
            DATES, ["20:50", "21:00", *(["20:30"] * 5)], strict=True,
        )
    ]
    assert all(row.event_spec["timing"]["timezone"] == "America/Los_Angeles" for row in events)
    assert all(any(ref.startswith("stm_") for ref in row.basis_refs) for row in events)
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(func.count(TemporalBinding.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 7


def test_mixed_message_and_image_stage_then_commit_without_review(session):
    _, _, lane, operations, binding, stage, commit, _ = _fixture(session)
    first = stage(1, [*operations, binding], initial=True)
    assert first["disposition"] == "ready_to_commit", first
    assert first["predicted_provider_operation_count"] == 7
    assert len(json.dumps(first, separators=(",", ":")).encode()) <= 16 * 1024
    assert not first.get("observation_required")
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert commit(2)["disposition"] == "committed"
    _assert_committed(session, lane)
    assert stage(1, [*operations, binding], initial=True) == {**first, "replayed": True}
    assert commit(2)["replayed"] is True
    _assert_committed(session, lane)


def test_recover_preserved_draft_under_original_scope_and_receipts(session):
    _, _, lane, operations, binding, stage, commit, review = _fixture(session)
    first = stage(1, operations, initial=True)
    assert first["disposition"] == "saved_with_errors"
    assert first["diagnostic_sample"][0]["code"] == "import_scope_required"
    request = session.scalar(select(SemanticRequest))
    original_scope = deepcopy(request.selected_option_binding)
    original_hash = request.authority_scope_hash
    old = session.scalar(select(ChangeSetRevision))
    old_effects = deepcopy(old.event_changes)
    repaired = stage(2, [binding])
    assert repaired["disposition"] == "ready_to_commit", repaired
    assert repaired["observation_required"] is True
    assert commit(3)["disposition"] == "draft_revision_conflict"
    review(4)
    assert commit(5)["disposition"] == "committed"
    assert request.selected_option_binding == original_scope
    assert request.authority_scope_hash == original_hash
    assert old.event_changes == old_effects and old.import_scope_json is None
    assert read_proof(session, request.ref_id)["historical_derivation_backfilled"] is False
    assert stage(1, operations, initial=True) == {**first, "replayed": True}
    assert stage(2, [binding]) == {**repaired, "replayed": True}
    _assert_committed(session, lane)


@pytest.mark.parametrize(
    "attack", ["eighth_event", "date", "duration", "location", "series", "lane"],
)
def test_field_provenance_cannot_expand_or_reinterpret_effects(session, attack):
    _, _, _, operations, binding, stage, _, _ = _fixture(session)
    assert stage(1, [*operations, binding], initial=True)["disposition"] == "ready_to_commit"
    changed = deepcopy(operations[0])
    spec = changed["action"]["create_spec"]["event_spec"]
    if attack == "eighth_event":
        changed["action"]["change_id"] = "extra-event"
    elif attack == "date":
        spec["timing"].update(start_local="2026-09-18T19:50:00", end_local="2026-09-18T20:50:00")
    elif attack == "duration":
        spec["timing"]["end_local"] = "2026-09-17T21:50:00"
    elif attack == "location":
        spec["location"] = "A different building"
    elif attack == "series":
        spec["recurrence"] = {"frequency": "weekly", "weekdays": ["TH"], "count": 10}
    else:
        changed["action"]["create_spec"]["lane_ref"] = new_public_ref("lane")
    with pytest.raises(DocketError) as error, session.begin_nested():
        stage(2, [changed])
    assert error.value.code == "field_evidence_effect_conflict"
    assert session.scalar(select(ChangeSet)).current_revision == 1
    assert session.scalar(select(func.count(Operation.id))) == 0


def test_bad_field_binding_retains_whole_draft_for_new_operation(session):
    _, _, lane, operations, binding, stage, commit, review = _fixture(session)
    bad = deepcopy(binding)
    bad["bindings"][0]["value"] = "Some other building"
    rejected = stage(1, [*operations, bad], initial=True)
    assert rejected["disposition"] == "saved_with_errors"
    assert len(session.scalar(select(ChangeSet)).staged_actions_json) == 14
    assert session.scalar(select(RequestFieldEvidence)) is None
    assert commit(2)["disposition"] == "rejected_validation"
    repaired = stage(3, [binding])
    assert repaired["disposition"] == "ready_to_commit", repaired
    review(4)
    assert commit(5)["disposition"] == "committed"
    _assert_committed(session, lane)


def test_shared_validation_rejects_missing_field_statement_and_changed_effect(session):
    _, service, _, operations, binding, stage, _, _ = _fixture(session)
    assert stage(1, [*operations, binding], initial=True)["disposition"] == "ready_to_commit"
    draft = session.scalar(select(ChangeSet))
    content = service.changesets.verify_execution_revision(draft)
    raw = content.model_dump(mode="json")
    raw["event_changes"][0]["basis_refs"] = operations[0]["action"]["basis_refs"]
    invalid = ChangeSetContent.model_validate(raw)
    errors = service.changesets._import_scope_errors(
        content=invalid, changes=[*invalid.event_changes, *invalid.lane_changes],
        session_utterance_refs=set(), request_ref=draft.semantic_request_ref,
    )
    assert any(row["code"] == "field_evidence_invalid" for row in errors)
    raw["event_changes"][0]["create_spec"]["event_spec"]["location"] = "Wrong"
    invalid = ChangeSetContent.model_validate(raw)
    errors = service.changesets._import_scope_errors(
        content=invalid, changes=[*invalid.event_changes, *invalid.lane_changes],
        session_utterance_refs=set(), request_ref=draft.semantic_request_ref,
    )
    assert any(row["code"] == "field_evidence_effect_conflict" for row in errors)


def test_recorded_binding_cannot_be_reinterpreted_by_a_later_stage(session):
    _, _, _, operations, binding, stage, _, _ = _fixture(session)
    stage(1, [*operations, binding], initial=True)
    changed = deepcopy(binding)
    changed["bindings"][0]["source_fragment_locator"] = {"region": "different_reading"}
    with pytest.raises(DocketError) as error, session.begin_nested():
        stage(2, [changed])
    assert error.value.code == "field_evidence_effect_conflict"
    assert session.scalar(select(ChangeSet)).current_revision == 1
    assert session.scalar(select(RequestFieldEvidence)).proof_json["bindings"] == [
        {**binding["bindings"][0], "source_fragment_hash": None},
    ]


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_field_proof_is_immutable(session, mutation):
    _, _, _, operations, binding, stage, _, _ = _fixture(session)
    stage(1, [*operations, binding], initial=True)
    session.commit()
    proof = session.scalar(select(RequestFieldEvidence))
    if mutation == "update":
        proof.proof_hash = "0" * 64
    else:
        session.delete(proof)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()


def test_mixed_structured_import_retains_exact_entry_coverage(session):
    _, _, lane, operations, binding, stage, commit, _ = _fixture(session)
    source = binding["bindings"][0]["source_ref"]
    entry = {"operation": "normalized_entry_upsert", "entry": {
        "entry_type": "scheduled_occurrence_entry", "import_entry_id": "source-row",
        "title": "Source schedule occurrence", "lane_ref": lane.ref_id,
        "timing": {"kind": "timed", "start_local": "2026-12-10T19:00:00",
                   "end_local": "2026-12-10T20:00:00", "timezone": "America/Los_Angeles"},
        "evidence": {"source_ref": source, "source_fragment_locator": {"row": 1},
                     "source_fragment_hash": "a" * 64,
                     "extractor_identifier": "hermes.native-vision",
                     "extractor_version": "fixture"},
    }}
    first = stage(1, [*operations, entry, binding], initial=True, scope_update={
        "normalized_entry_types": ["scheduled_occurrence_entry"],
        "selected_entry_ids": ["source-row"],
    })
    assert first["disposition"] == "ready_to_commit", first["diagnostic_sample"]
    draft = session.scalar(select(ChangeSet))
    assert len(draft.import_scope_json["entry_coverage"]) == 1
    assert first["source_interpretation"]["selected_entry_count"] == 1
    assert commit(2)["disposition"] == "committed"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 8
    assert session.scalar(select(func.count(Item.id))) == 1
    assert session.scalar(select(func.count(TemporalBinding.id))) == 1


def test_recompile_keeps_field_bindings_and_exact_effects(session):
    _, _, lane, operations, binding, stage, commit, review = _fixture(session)
    first = stage(1, [*operations, binding], initial=True)
    assert first["disposition"] == "ready_to_commit"
    migrated = stage(2, [{"operation": "draft_recompile"}])
    assert migrated["disposition"] == "ready_to_commit", migrated
    assert migrated["observation_required"]
    review(3)
    assert commit(4)["disposition"] == "committed"
    _assert_committed(session, lane)


def test_missing_or_false_field_proof_is_not_an_import_exemption(session):
    _, service, _, operations, binding, stage, commit, _ = _fixture(session)
    partial = deepcopy(binding)
    partial["bindings"][0]["targets"].pop()
    failed = stage(1, [*operations, partial], initial=True)
    assert failed["disposition"] == "saved_with_errors"
    assert failed["diagnostic_sample"][0]["constraint"] == "every_cited_source_has_field_binding"
    assert session.scalar(select(RequestFieldEvidence)) is None
    assert commit(2)["disposition"] == "rejected_validation"
    assert session.scalar(select(func.count(Operation.id))) == 0
    # Even a maliciously crafted import envelope on the shared service retains
    # the full structured Calendar coverage requirement without the durable proof.
    draft = session.scalar(select(ChangeSet))
    content = service.changesets.verify_execution_revision(draft)
    raw = content.model_dump(mode="json")
    raw["import_scope"] = {
        "mode": "operator_explicit", "source_refs": [binding["bindings"][0]["source_ref"]],
        "authorized_effects": ["canonical_event", "lane_routing_decision"],
        "authority_statement_refs": ["stm_01ARZ3NDEKTSV4RRFFQ69G5FAV"],
    }
    forged = ChangeSetContent.model_validate(raw)
    errors = service.changesets._import_scope_errors(
        content=forged, changes=[*forged.event_changes, *forged.lane_changes],
        session_utterance_refs=set(), request_ref=draft.semantic_request_ref,
    )
    assert any(row["code"] == "import_entry_coverage_required" for row in errors)

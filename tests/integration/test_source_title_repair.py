from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from test_attachment_evidence import _pdf_bytes, _request, _text_service
from test_changeset_assembly import _admit

from docket.domain.canonical import sha256_json
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
    SemanticRequest,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.provenance import ProvenanceService

TITLES = ["2026 Fall Career Fair", "2026 Fall Career Fair", "2026 Business Career Fair"]
LOCATION = "Cal Poly Recreation Center, Building 43"


def _fixture(session, monkeypatch, *, source_title=True, faulty_fields="both"):
    import docket.services.changeset_assembly as assembly

    texts = [f"{title}{'' if source_title else 'ness'} - September {16 + index}, 2026; "
             f"10:00 AM to {2 if index == 2 else 3}:00 PM; {LOCATION}."
             for index, title in enumerate(TITLES)]
    captured = ProvenanceService(session).capture_operator_utterance(_request(
        message_id="1542999000000000621", content=_pdf_bytes(*texts),
        filename="career-fairs.pdf", media_type="application/pdf",
    ).model_copy(update={"verbatim_text": "Add these three fairs to the Meetings calendar."}))
    utterance = session.scalar(select(OperatorUtterance).where(
        OperatorUtterance.ref_id == captured["ref"],
    ))
    source_ref = captured["attachments"][0]["ref"]
    read = _text_service(session).read_pdf_text(
        source_ref=source_ref, cursor=None, max_text_bytes=8192, page_limit=3,
    )
    account = ProviderAccount(
        provider="google", external_account_id="source-repair-smoke",
        capabilities=["google_calendar"], enabled=True,
    )
    session.add(account)
    session.flush()
    lanes = [CalendarLane(
        account_id=account.id, lane=name, display_name=name.title(), color_hex="#3367D6",
        calendar_id=f"{name}@example.com", status="active", basis_refs=[utterance.ref_id],
        created_by_changeset_ref="chg_01M1A100000000000000000000",
    ) for name in ("meetings", "unrelated")]
    session.add_all(lanes)
    session.flush()
    operations = []
    for index, fragment in enumerate(read["items"]):
        operations.append({
            "operation": "normalized_entry_upsert", "entry": {
                "entry_type": "scheduled_occurrence_entry", "import_entry_id": f"fair-{index}",
                "title": TITLES[index], "location": LOCATION, "lane_ref": lanes[0].ref_id,
                "timing": {"kind": "timed", "start_local": f"2026-09-{16 + index}T10:00:00",
                           "end_local": f"2026-09-{16 + index}T{14 if index == 2 else 15}:00:00",
                           "timezone": "America/Los_Angeles"},
                "evidence": {"source_ref": source_ref,
                             "source_fragment_locator": fragment["source_fragment_locator"],
                             "source_fragment_hash": fragment["source_fragment_hash"],
                             "extractor_identifier": read["extractor_identifier"],
                             "extractor_version": read["extractor_version"]},
            },
        })
    trace = new_public_ref("trace")
    original_compiler = assembly.compile_normalized_entry

    def faulty_compiler(*args, **kwargs):
        compiled = original_compiler(*args, **kwargs)
        actions = deepcopy(list(compiled.actions))
        for action in actions:
            if action["mutation_type"] == "canonical_event_create":
                if faulty_fields in {"both", "canonical"}:
                    action["create_spec"]["title"] = "Incorrect duplicated title"
                if faulty_fields in {"both", "provider"}:
                    action["create_spec"]["event_spec"]["title"] = "Incorrect duplicated title"
        return replace(compiled, actions=tuple(actions))

    def admit(ordinal, tool):
        return _admit(
            session, utterance=utterance, trace_ref=trace, call_id=f"repair-{ordinal}",
            ordinal=ordinal, tool_name=tool, argument_hash=str(ordinal) * 64,
        )

    service = ChangeSetAssemblyService(session)
    with monkeypatch.context() as patch:
        patch.setattr(assembly, "compile_normalized_entry", faulty_compiler)
        first = service.stage(StageChangesInput.model_validate({
            "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
            "assembly_scope": {
                "resolved_intent": {"intent": "add three source fairs"},
                "normalized_entry_types": ["scheduled_occurrence_entry"],
                "source_refs": [source_ref], "target_refs": [lanes[0].ref_id],
            }, "patch": {"operations": operations},
        }), assembly_operation_token=admit(1, "docket_stage_changes"),
            assembly_argument_hash="1" * 64)
    assert first["disposition"] == "saved_with_errors"
    draft = session.scalar(select(ChangeSet))
    assert len(draft.normalized_entries_json) == 3
    assert any(row["code"] == "import_entry_calendar_title_mismatch"
               for row in draft.validation_errors)
    rejected = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=admit(2, "docket_commit_changeset"),
        assembly_argument_hash="2" * 64,
    )
    assert rejected["disposition"] == "rejected_validation"
    return utterance, service, draft, lanes, admit, original_compiler


def _repair(utterance, service, admit):
    return service.stage(StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "patch": {"operations": [{"operation": "draft_recompile"}]},
    }), assembly_operation_token=admit(3, "docket_stage_changes"), assembly_argument_hash="3" * 64)


@pytest.mark.parametrize("faulty_fields", ["both", "canonical", "provider"])
def test_source_title_coalescence_repairs_same_request_and_commits_exact_three_events(
    session, monkeypatch, faulty_fields,
):
    utterance, service, draft, lanes, admit, _ = _fixture(
        session, monkeypatch, faulty_fields=faulty_fields,
    )
    request = session.scalar(select(SemanticRequest))
    authority_hash, request_ref = request.authority_scope_hash, request.ref_id
    original_effects = deepcopy(draft.event_changes)
    result = _repair(utterance, service, admit)
    assert result["disposition"] == "ready_to_commit"
    assert result["compiler_migration"]["source_title_repair_count"] == 3
    assert result["compiler_migration"]["dependency_comparison"] == "exact_existing_change_ids"
    assert draft.current_revision == 2
    proof = draft.compiler_manifest_json["source_title_repair_proofs"]
    assert sha256_json(proof) == result["compiler_migration"]["source_title_proof_hash"]
    assert "text" not in proof[0]["evidence"]
    assert session.scalar(select(ChangeSetRevision).where(
        ChangeSetRevision.change_set_id == draft.id, ChangeSetRevision.revision == 1,
    )).event_changes == original_effects
    assert request.authority_scope_hash == authority_hash and request.ref_id == request_ref
    # Explicit compiler migration requires reobservation, but not a full diff read.
    service.review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
        assembly_operation_token=admit(4, "docket_review_changeset"),
        assembly_argument_hash="4" * 64,
    )
    receipt = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=admit(5, "docket_commit_changeset"),
        assembly_argument_hash="5" * 64,
    )
    assert receipt["disposition"] == "committed"
    events = sorted(session.scalars(select(CanonicalEvent)),
                    key=lambda event: event.event_spec["timing"]["start_local"])
    assert len(events) == 3
    assert [event.title for event in events] == TITLES
    assert [event.event_spec["timing"]["start_local"] for event in events] == [
        f"2026-09-{day}T10:00:00" for day in (16, 17, 18)
    ]
    assert [event.event_spec["timing"]["end_local"] for event in events] == [
        "2026-09-16T15:00:00", "2026-09-17T15:00:00", "2026-09-18T14:00:00",
    ]
    assert all(event.event_spec["location"] == LOCATION for event in events)
    assert all(event.event_spec["timing"]["timezone"] == "America/Los_Angeles"
               for event in events)
    assert all(event.lane_ref == lanes[0].ref_id for event in events)
    assert session.scalar(select(func.count(Item.id))) == 3
    assert session.scalar(select(func.count(Operation.id))) == 3
    # A lost response replays the exact migration outcome, even after commit.
    # It does not reread source bytes, create a revision or repeat provider work.
    from docket.services.attachment_evidence import AttachmentTextService

    def unexpected_verification(*args, **kwargs):
        raise AssertionError("replay must not reapply source repair")

    monkeypatch.setattr(AttachmentTextService, "verify_fragment", unexpected_verification)
    assert _repair(utterance, service, admit) == {**result, "replayed": True}
    assert draft.current_revision == 2
    assert session.scalar(select(func.count(Operation.id))) == 3


@pytest.mark.parametrize("attack", ["title", "date", "destination", "fourth_event", "series"])
def test_title_repair_cannot_expand_or_change_other_selected_effects(session, monkeypatch, attack):
    import docket.services.changeset_recompile as recompile

    utterance, service, draft, lanes, admit, compiler = _fixture(session, monkeypatch)
    original = deepcopy(draft.event_changes)

    def changed_compiler(*args, **kwargs):
        compiled = compiler(*args, **kwargs)
        if args[0].import_entry_id != "fair-0":
            return compiled
        actions = deepcopy(list(compiled.actions))
        event = next(row for row in actions if row["mutation_type"] == "canonical_event_create")
        event_spec = event["create_spec"]["event_spec"]
        if attack == "title":
            event_spec["title"] = "A different fair"
        elif attack == "date":
            event_spec["timing"].update(start_local="2026-10-01T10:00:00",
                                        end_local="2026-10-01T15:00:00")
        elif attack == "destination":
            event["create_spec"]["lane_ref"] = lanes[1].ref_id
            event_spec["calendar_lane"] = lanes[1].lane
        elif attack == "series":
            event_spec["recurrence"] = {"frequency": "weekly", "weekdays": ["WE"], "count": 2}
        else:
            extra = deepcopy(event)
            extra["change_id"] = "fourth-event"
            extra["create_spec"]["canonical_key"] = "unrequested-fourth-event"
            actions.append(extra)
        return replace(compiled, actions=tuple(actions))

    monkeypatch.setattr(recompile, "compile_normalized_entry", changed_compiler)
    with pytest.raises(DocketError) as error:
        _repair(utterance, service, admit)
    assert error.value.code == "draft_recompile_semantic_conflict"
    assert error.value.details["authority_preserved"] is True
    assert draft.event_changes == original and draft.current_revision == 1
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0


def test_title_repair_requires_literal_retained_evidence_not_a_model_claim(session, monkeypatch):
    utterance, service, draft, _, admit, _ = _fixture(session, monkeypatch, source_title=False)
    with pytest.raises(DocketError) as error:
        _repair(utterance, service, admit)
    assert error.value.code == "source_title_repair_unproved"
    assert error.value.details["constraint"] == "selected_title_is_literal_source_text"
    assert draft.current_revision == 1
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0


def test_title_coalescence_preserves_other_patch_field_presence(session, monkeypatch):
    from docket.schemas.authority import ItemModify
    from docket.services.request_specifications import read_request_proposal
    from docket.services.source_title_repair import coalesce_source_titles

    utterance, service, draft, _, _, _ = _fixture(session, monkeypatch)
    original = service.changesets.verify_execution_revision(draft, for_migration=True)
    original.tracked_context_changes.append(ItemModify.model_validate({
        "mutation_type": "item_modify", "change_id": "unrelated-patch",
        "action": "update", "object_type": "item", "object_ref": new_public_ref("item"),
        "payload": {"description": None}, "affected_fields": ["description"],
        "basis_refs": [utterance.ref_id],
    }))
    repaired, proof = coalesce_source_titles(
        session, prior=original, proposal=read_request_proposal(
            session, semantic_request_ref=draft.semantic_request_ref, version=1,
        ),
    )
    assert len(proof) == 3
    assert repaired.tracked_context_changes[-1].payload.model_fields_set == {"description"}
    assert repaired.tracked_context_changes[-1].payload.description is None
    assert repaired.tracked_context_changes == original.tracked_context_changes
    assert original.event_changes[0].create_spec.title == "Incorrect duplicated title"

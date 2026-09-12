import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_changeset_assembly import (
    _admit,
    _item_stage,
    _schedule_context,
    _schedule_stage,
    _utterance,
)

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import ChangeSet, SemanticRequest, SemanticRequestSpecification
from docket.schemas.assembly import StageChangesInput
from docket.schemas.request_specifications import RequestSpecificationProposal
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.request_specifications import read_request_proposal


def _stage(session, utterance, trace_ref, ordinal, payload):
    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id=f"spec-stage-{ordinal}",
        ordinal=ordinal, tool_name="docket_stage_changes", argument_hash=str(ordinal) * 64,
    )
    return ChangeSetAssemblyService(session).stage(
        payload, assembly_operation_token=token, assembly_argument_hash=str(ordinal) * 64,
    )


def test_exact_proposals_preserve_failed_entries_without_claiming_source_authority(
    session, monkeypatch,
):
    utterance = _utterance("1542799000000000941")
    source, lane = _schedule_context(session, utterance, suffix="specification")
    payload = _schedule_stage(
        utterance, source_ref=source.ref_id, lane_ref=lane.ref_id,
        start_index=0, count=3, include_scope=True,
    )
    original = payload.model_dump(mode="json")
    trace = new_public_ref("trace")
    lane_lookup = ChangeSetAssemblyService._entry_lane

    def temporarily_unavailable(service, entry, actions):
        if entry.import_entry_id.endswith("01"):
            raise DocketError(code="fixture_binding_unavailable", message="Synthetic failure")
        return lane_lookup(service, entry, actions)

    with monkeypatch.context() as patch:
        patch.setattr(ChangeSetAssemblyService, "_entry_lane", temporarily_unavailable)
        failed = _stage(session, utterance, trace, 1, payload)
    assert failed["disposition"] == "saved_with_errors"
    request = session.scalar(select(SemanticRequest))
    authority = request.authority_scope_hash
    first = read_request_proposal(session, semantic_request_ref=request.ref_id, version=1)
    assert first.interpretation_state == "pending_evidence_validation"
    assert len(first.normalized_entries) == 3
    assert first.direct_actions == []  # No compiler-owned Item/Time/Event duplication.
    assert first.normalized_entries[1].lane_ref == lane.ref_id
    assert first.source_bindings[0].attachment_content_hash == source.content_hash
    assert first.source_bindings[0].evidence_state == "attachment_recorded"
    assert [binding.utterance_ref for binding in first.originating_utterances] == [utterance.ref_id]
    first_snapshot = first.model_dump(mode="json", exclude_none=True)
    # Repair gets a new operation/revision, not a mutation of original evidence.
    original["assembly_scope"] = None
    repaired = _stage(session, utterance, trace, 2, StageChangesInput.model_validate(original))
    assert repaired["disposition"] == "ready_to_commit"
    second = read_request_proposal(session, semantic_request_ref=request.ref_id, version=2)
    assert second.normalized_entries[1].lane_ref == lane.ref_id
    assert second.interpretation_state == "pending_evidence_validation"
    assert request.authority_scope_hash == authority
    assert request.authority_availability == "available"
    # An operation replay is not another interpretation or an authority grant.
    replay = _stage(session, utterance, trace, 2, StageChangesInput.model_validate(original))
    assert replay == {**repaired, "replayed": True}
    assert session.scalar(select(func.count(SemanticRequestSpecification.version))) == 2
    with pytest.raises(ValidationError):
        RequestSpecificationProposal.model_validate({
            **second.model_dump(mode="json"), "interpretation_state": "verified",
        })
    assert read_request_proposal(
        session, semantic_request_ref=request.ref_id, version=1,
    ).model_dump(mode="json", exclude_none=True) == first_snapshot
    with pytest.raises(DocketError) as missing:
        read_request_proposal(session, semantic_request_ref=request.ref_id, version=3)
    assert missing.value.code == "request_specification_not_found"
    session.commit()
    assert session.scalar(select(func.count(SemanticRequestSpecification.version))) == 2


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_request_specification_orm_is_immutable(session, mutation):
    utterance = _utterance("1542799000000000942")
    session.add(utterance)
    session.flush()
    _stage(session, utterance, new_public_ref("trace"), 1, _item_stage(utterance))
    session.commit()
    row = session.scalar(select(SemanticRequestSpecification))
    assert row.specification_hash == sha256_json(row.specification_json)
    assert len(row.specification_json["direct_actions"]) == 1
    assert "expected_versions" not in row.specification_json
    assert "compiler_manifest" not in row.specification_json
    if mutation == "update":
        row.specification_json = {**row.specification_json, "interpretation_state": "verified"}
    else:
        session.delete(row)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()
    assert session.scalar(select(ChangeSet)).current_revision == 1


def test_invalid_specification_digest_cannot_be_used_as_a_bound_proposal(session):
    utterance = _utterance("1542799000000000943")
    session.add(utterance)
    session.flush()
    _stage(session, utterance, new_public_ref("trace"), 1, _item_stage(utterance))
    session.flush()
    row = session.scalar(select(SemanticRequestSpecification))
    # Bulk SQL deliberately bypasses the ORM (PostgreSQL trigger is tested separately).
    session.execute(SemanticRequestSpecification.__table__.update().values(
        specification_hash="0" * 64,
    ))
    session.expire_all()
    with pytest.raises(DocketError) as error:
        read_request_proposal(session, semantic_request_ref=row.semantic_request_ref, version=1)
    assert error.value.code == "request_specification_integrity_mismatch"

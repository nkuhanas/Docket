from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_changeset_assembly import _admit, _utterance

from docket.domain.public_refs import new_public_ref
from docket.models import ChangeSet, ChangeSetRevision, Item, Task
from docket.schemas.assembly import StageChangesInput
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.request_specifications import read_request_proposal


@pytest.mark.parametrize("kind", ["clear_only", "retitle_and_clear", "retitle_only", "reopen_task"])
def test_staged_patch_retains_exact_fields_through_restart_and_commit(session, kind):
    utterance = _utterance("1542799000000000741")
    session.add(utterance)
    session.flush()
    item = Item(title="Original", description="Keep unless explicitly cleared",
                kind="test.request", basis_refs=[utterance.ref_id],
                created_by_changeset_ref=new_public_ref("chg"))
    session.add(item)
    session.flush()
    target = item
    object_type = "item"
    payload = {"description": None}
    if kind == "retitle_and_clear":
        payload["title"] = "Retitled"
    elif kind == "retitle_only":
        payload = {"title": "Retitled"}
    elif kind == "reopen_task":
        target = Task(item_ref=item.ref_id, title="Follow up", task_state="completed",
                      completed_at=datetime(2026, 9, 10, tzinfo=UTC),
                      description="Task description", basis_refs=[utterance.ref_id],
                      created_by_changeset_ref=new_public_ref("chg"))
        session.add(target)
        session.flush()
        object_type = "task"
        payload = {"task_state": "in_progress", "completed_at": None}
    target_ref = target.ref_id
    action = {"mutation_type": f"{object_type}_modify", "change_id": "edit",
              "action": "update", "object_type": object_type, "object_ref": target_ref,
              "payload": payload, "affected_fields": list(payload),
              "basis_refs": [utterance.ref_id]}
    request = StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "assembly_scope": {"resolved_intent": {"correction": kind},
                           "allowed_mutation_types": [f"{object_type}_modify"],
                           "target_refs": [target_ref]},
        "expected_versions": {target_ref: target.version},
        "patch": {"operations": [{"operation": "action_upsert", "action": action}]},
    })
    trace_ref = new_public_ref("trace")

    def admit(ordinal, tool):
        return _admit(session, utterance=utterance, trace_ref=trace_ref,
                      call_id=f"patch-{ordinal}", ordinal=ordinal, tool_name=tool,
                      argument_hash=str(ordinal) * 64)

    staged = ChangeSetAssemblyService(session).stage(
        request, assembly_operation_token=admit(1, "docket_stage_changes"),
        assembly_argument_hash="1" * 64,
    )
    assert staged["disposition"] == "ready_to_commit"
    draft = session.scalar(select(ChangeSet))
    assert draft.staged_actions_json[0]["payload"] == payload
    assert draft.tracked_context_changes[0]["payload"] == payload
    revision = session.scalar(select(ChangeSetRevision))
    assert revision.tracked_context_changes[0]["payload"] == payload
    proposal = read_request_proposal(session, semantic_request_ref=draft.semantic_request_ref,
                                    version=revision.revision)
    assert proposal.direct_actions[0].payload.model_dump(exclude_unset=True) == payload
    preview = revision.compiler_manifest_json["canonical_patch_preview"]["effects"][0]
    assert preview["after"] == payload
    assert preview["observed_version"] == preview["expected_version"] == 1
    if kind == "reopen_task":
        assert preview["before"]["task_state"] == "completed"
        assert preview["before"]["completed_at"] is not None
    elif "description" in payload:
        assert preview["before"]["description"] == "Keep unless explicitly cleared"
    session.commit()
    session.expire_all()

    receipt = ChangeSetAssemblyService(session).commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=admit(2, "docket_commit_changeset"),
        assembly_argument_hash="2" * 64,
    )
    assert receipt["disposition"] == "committed"
    session.flush()
    session.refresh(target)
    assert target.version == 2
    for field, value in preview["after"].items():
        assert getattr(target, field) == value
    if kind == "reopen_task":
        assert target.task_state == "in_progress" and target.completed_at is None
        assert target.title == "Follow up" and target.description == "Task description"
    else:
        assert target.title == ("Original" if kind == "clear_only" else "Retitled")
        assert target.description == (
            "Keep unless explicitly cleared" if kind == "retitle_only" else None
        )
        assert target.kind == "test.request"

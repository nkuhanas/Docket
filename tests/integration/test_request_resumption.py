"""Original request identity survives a change of model-facing protocol."""

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AssemblyExecution,
    AssemblyOperation,
    ChangeSet,
    ChangeSetRevision,
    Entity,
    Item,
    OperatorUtterance,
    SemanticRequest,
    SemanticRequestAttempt,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.authority import ChangeSetContent
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.interactive_authority import InteractiveAuthorityService


def _utterance(message_id):
    settings = get_settings()
    text = "Track this request."
    return OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}", transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=(
            f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}"
        ),
        said_at=datetime.now(UTC), verbatim_text=text,
        content_hash=sha256(text.encode()).hexdigest(),
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
    )


def _admit(session, *, utterance, trace_ref, call_id, ordinal, tool_name, argument_hash):
    settings = get_settings()
    result = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id, trace_ref=trace_ref, upstream_tool_call_id=call_id,
        trace_ordinal=ordinal, tool_name=tool_name, argument_hash=argument_hash,
        guild_id=settings.discord_guild_id, channel_id=settings.chat_channel_id,
        source_message_id=utterance.request_key.split(":")[3],
        actor_id=settings.operator_discord_user_id,
    )
    return result["assembly_operation_token"]


def _item_stage(utterance):
    return StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "assembly_scope": {
            "resolved_intent": {"kind": "track"}, "allowed_mutation_types": ["item_create"],
            "planned_create_types": ["item"],
        },
        "patch": {"operations": [{"operation": "action_upsert", "action": {
            "mutation_type": "item_create", "change_id": "tracked-request",
            "action": "create", "object_type": "item",
            "affected_fields": ["title"], "basis_refs": [utterance.ref_id],
            "create_spec": {"title": "Tracked request"},
        }}]},
    })


def _direct_request(session, *, completed, utterance=None):
    utterance = utterance or _utterance("1542799000000000881")
    session.add(utterance)
    session.flush()
    if completed:
        staged = _item_stage(utterance)
        content = ChangeSetContent(
            basis_refs=[utterance.ref_id],
            tracked_context_changes=[staged.patch.operations[0].action],
        )
    else:
        # Preserve a genuine service-produced failed direct request, not a
        # fabricated success row or a test-only assembly binding.
        entity = Entity(
            entity_kind="organization", display_name="Pacheco Post",
            normalized_name="pacheco post", canonical_status="active",
            attributes_json={}, basis_refs=[utterance.ref_id],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(entity)
        session.flush()
        content = ChangeSetContent.model_validate({
            "basis_refs": [utterance.ref_id], "expected_versions": {entity.ref_id: 2},
            "registry_changes": [{
                "mutation_type": "entity_modify", "change_id": "rename",
                "action": "update", "object_type": "entity",
                "object_ref": entity.ref_id, "payload": {"display_name": "Mail Center"},
                "affected_fields": ["display_name"], "basis_refs": [utterance.ref_id],
            }],
        })
    result = InteractiveAuthorityService(session).process_turn(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        actor_id=get_settings().operator_discord_user_id,
        intent_session_ref=None, expected_session_version=None, statements=[], relations=[],
        resolved_intent_json={"kind": "fixture"}, blocking_clarifications=[], content=content,
        changeset_ref=None, expected_changeset_version=None,
    )
    assert result["state"] == ("committed" if completed else "blocked_version")
    request = session.scalar(select(SemanticRequest))
    assert request.selected_option_binding["kind"] == "freeform_turn"
    session.flush()
    return utterance, request, session.scalar(select(ChangeSet))


@pytest.mark.parametrize("tool", ["docket_stage_changes", "docket_commit_changeset"])
def test_committed_direct_request_recovers_receipt_after_restart(session, monkeypatch, tool):
    utterance, request, changeset = _direct_request(session, completed=True)
    refs = utterance.ref_id, request.ref_id, changeset.ref_id
    receipt = deepcopy(changeset.commit_receipt_json)
    binding = deepcopy(request.selected_option_binding)
    session.commit()
    session.expire_all()
    utterance = session.scalar(select(OperatorUtterance).where(OperatorUtterance.ref_id == refs[0]))
    token = _admit(
        session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="receipt-recovery",
        ordinal=1, tool_name=tool, argument_hash="a" * 64,
    )
    service = ChangeSetAssemblyService(session)

    def never_mutate(*args, **kwargs):
        raise AssertionError("Committed recovery cannot execute or compile again")

    monkeypatch.setattr(service.changesets, "commit", never_mutate)
    monkeypatch.setattr(service, "_write_revision", never_mutate)
    if tool == "docket_stage_changes":
        # Even a stale model's unrelated assembly scope cannot replace the
        # already-committed request or force a new request into existence.
        result = service.stage(
            _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="a" * 64,
        )
        assert result == {**receipt, "disposition": "already_committed"}
    else:
        result = service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            assembly_operation_token=token, assembly_argument_hash="a" * 64,
        )
        assert result == receipt
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1
    assert session.scalar(select(func.count(ChangeSet.id))) == 1
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 1
    assert session.scalar(select(func.count(Item.id))) == 1
    assert session.scalar(select(SemanticRequest)).selected_option_binding == binding
    execution = session.scalar(select(AssemblyExecution))
    operation = session.scalar(select(AssemblyOperation))
    assert execution.semantic_request_ref == operation.semantic_request_ref == refs[1]
    assert operation.state == "completed"
    assert operation.result_json == result


@pytest.mark.parametrize("tool", ["docket_stage_changes", "docket_commit_changeset"])
@pytest.mark.parametrize("admit_before_request", [False, True])
def test_unfinished_direct_request_cannot_be_bypassed_by_staging(
    session, tool, admit_before_request,
):
    if admit_before_request:
        original = _utterance("1542799000000000881")
        session.add(original)
        session.flush()
        token = _admit(
            session, utterance=original, trace_ref=new_public_ref("trace"), call_id="early",
            ordinal=1, tool_name=tool, argument_hash="b" * 64,
        )
        utterance, request, changeset = _direct_request(
            session, completed=False, utterance=original,
        )
    else:
        utterance, request, changeset = _direct_request(session, completed=False)
        token = _admit(
            session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="resume",
            ordinal=1, tool_name=tool, argument_hash="b" * 64,
        )
    original = deepcopy(request.selected_option_binding)
    revision, authority = changeset.current_revision, request.authority_scope_hash
    service = ChangeSetAssemblyService(session)
    with pytest.raises(DocketError) as error:
        if tool == "docket_stage_changes":
            service.stage(
                _item_stage(utterance), assembly_operation_token=token,
                assembly_argument_hash="b" * 64,
            )
        else:
            service.commit(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                assembly_operation_token=token, assembly_argument_hash="b" * 64,
            )
    assert error.value.code == "semantic_request_migration_required"
    assert error.value.details["authority_preserved"] is True
    assert error.value.details["semantic_request_ref"] == request.ref_id
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1
    assert session.scalar(select(func.count(ChangeSet.id))) == 1
    assert session.scalar(select(func.count(Item.id))) == 0
    assert request.selected_option_binding == original
    assert request.authority_scope_hash == authority
    assert request.authority_availability == "available"
    assert changeset.current_revision == revision
    assert session.scalar(select(AssemblyExecution)).semantic_request_ref == request.ref_id


@pytest.mark.parametrize("availability", ["cancelled", "superseded", "invalidated_by_state"])
def test_unavailable_request_cannot_be_reopened_by_another_execution(session, availability):
    utterance, request, changeset = _direct_request(session, completed=False)
    request.authority_availability = availability
    session.flush()
    token = _admit(
        session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="terminal",
        ordinal=1, tool_name="docket_stage_changes", argument_hash="c" * 64,
    )
    with pytest.raises(DocketError) as error:
        ChangeSetAssemblyService(session).stage(
            _item_stage(utterance), assembly_operation_token=token,
            assembly_argument_hash="c" * 64,
        )
    assert error.value.code == "semantic_request_authority_unavailable"
    assert error.value.details["authority_availability"] == availability
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1
    assert session.scalar(select(func.count(ChangeSet.id))) == 1
    assert request.authority_availability == availability
    assert changeset.state != "committed"


def test_preserved_direct_revision_remains_reviewable_without_mutation(session):
    utterance, request, changeset = _direct_request(session, completed=False)
    token = _admit(
        session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="review-old",
        ordinal=1, tool_name="docket_review_changeset", argument_hash="d" * 64,
    )
    result = ChangeSetAssemblyService(session).review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
        assembly_operation_token=token, assembly_argument_hash="d" * 64,
    )
    assert result["disposition"] == "reviewed"
    attempt = session.scalar(select(SemanticRequestAttempt).where(
        SemanticRequestAttempt.execution_trace_ref.is_not(None),
    ))
    assert attempt.observed_changeset_ref == changeset.ref_id
    assert attempt.observed_draft_revision == changeset.current_revision
    assert request.authority_availability == "available"
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 1


def test_ambiguous_original_requests_require_resolution_not_latest_selection(session):
    utterance, first, _changeset = _direct_request(session, completed=False)
    second = SemanticRequest(
        intent_session_id=first.intent_session_id, intent_session_ref=first.intent_session_ref,
        authority_scope_hash="f" * 64, current_precondition_hash=first.current_precondition_hash,
        origin_utterance_refs=[utterance.ref_id], selected_option_binding={
            "kind": "freeform_assembly",
            "scope": _item_stage(utterance).assembly_scope.model_dump(),
        },
    )
    session.add(second)
    session.flush()
    with pytest.raises(DocketError) as error, session.begin_nested():
        _admit(
            session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="ambiguous",
            ordinal=1, tool_name="docket_stage_changes", argument_hash="e" * 64,
        )
    assert error.value.code == "assembly_resume_ambiguous"
    assert error.value.details["request_count"] == 2
    assert session.scalar(select(func.count(AssemblyExecution.id))) == 0
    assert session.scalar(select(func.count(ChangeSet.id))) == 1
    assert first.authority_availability == second.authority_availability == "available"


def test_recorded_failure_replay_does_not_gain_a_later_request_binding(session):
    utterance = _utterance("1542799000000000882")
    session.add(utterance)
    session.flush()
    token = _admit(
        session, utterance=utterance, trace_ref=new_public_ref("trace"), call_id="no-scope",
        ordinal=1, tool_name="docket_stage_changes", argument_hash="f" * 64,
    )
    service = ChangeSetAssemblyService(session)
    invalid = _item_stage(utterance).model_copy(update={"assembly_scope": None})
    with pytest.raises(DocketError) as error:
        service.stage(invalid, assembly_operation_token=token, assembly_argument_hash="f" * 64)
    assert error.value.code == "assembly_scope_required"
    recorded = service.reject_admitted_operation(
        token=token, argument_hash="f" * 64, operation_kind="stage",
        utterance_ref=utterance.ref_id, error=error.value,
    )
    _direct_request(session, completed=True, utterance=utterance)
    original_attempt_count = session.scalar(select(func.count(SemanticRequestAttempt.id)))
    replay = service.stage(invalid, assembly_operation_token=token, assembly_argument_hash="f" * 64)
    assert replay == {**recorded, "replayed": True}
    operation = session.scalar(select(AssemblyOperation))
    execution = session.scalar(select(AssemblyExecution))
    assert operation.semantic_request_ref is execution.semantic_request_ref is None
    assert session.scalar(select(func.count(SemanticRequestAttempt.id))) == original_attempt_count

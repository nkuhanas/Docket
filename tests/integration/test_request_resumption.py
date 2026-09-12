"""Original request identity survives a change of model-facing protocol."""

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AssemblyExecution,
    AssemblyOperation,
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    Entity,
    IntentSession,
    Item,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    RequestAssemblyAdoption,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
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


def _failed_item_request(session, monkeypatch, *, with_source=False):
    utterance = _utterance("1542799000000000883")
    session.add(utterance)
    session.flush()
    stage = _item_stage(utterance)
    content = ChangeSetContent(
        basis_refs=[utterance.ref_id], tracked_context_changes=[stage.patch.operations[0].action],
    )
    if with_source:
        source = Source(
            source_kind="external", external_ref="adoption-fixture-source",
            observed_at=datetime.now(UTC), content_hash=sha256_json({"fixture": "original"}),
        )
        session.add(source)
        session.flush()
        content.basis_refs.append(source.ref_id)
        content.tracked_context_changes[0].create_spec.source_refs = [source.ref_id]
    return _retain_failed_request(session, monkeypatch, utterance, content)


def _retain_failed_request(session, monkeypatch, utterance, content):
    service = InteractiveAuthorityService(session)
    with monkeypatch.context() as patch:
        patch.setattr(service.changesets, "_validate", lambda *a, **kw: [{
            "code": "fixture_implementation_validation", "category": "implementation_validation",
        }])
        result = service.process_turn(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None, expected_session_version=None, statements=[], relations=[],
            resolved_intent_json={"kind": "fixture"}, blocking_clarifications=[], content=content,
            changeset_ref=None, expected_changeset_version=None,
        )
    assert result["state"] == "blocked_validation"
    return utterance, session.scalar(select(SemanticRequest)), session.scalar(select(ChangeSet))


def _review_adoption(session, utterance, trace, ordinal):
    argument_hash = sha256_json({"review": ordinal})
    token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=ordinal,
        call_id=f"review-{ordinal}", tool_name="docket_review_changeset",
        argument_hash=argument_hash,
    )
    return ChangeSetAssemblyService(session).review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
        assembly_operation_token=token, assembly_argument_hash=argument_hash,
    )


def _adopt(session, utterance, trace, ordinal):
    request = StageChangesInput(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        patch={"operations": [{"operation": "draft_adopt"}]},
    )
    digest = sha256_json(request.model_dump(mode="json"))
    token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=ordinal,
        call_id=f"adopt-{ordinal}", tool_name="docket_stage_changes", argument_hash=digest,
    )
    return ChangeSetAssemblyService(session).stage(
        request, assembly_operation_token=token, assembly_argument_hash=digest,
    )


def _commit_adoption(session, utterance, trace, ordinal):
    digest = sha256_json({"commit": ordinal})
    token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=ordinal,
        call_id=f"commit-{ordinal}", tool_name="docket_commit_changeset", argument_hash=digest,
    )
    return ChangeSetAssemblyService(session).commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=token, assembly_argument_hash=digest,
    )


def test_direct_request_adoption_preserves_evidence_reobserves_and_commits_once(
    session, monkeypatch,
):
    utterance, request, changeset = _failed_item_request(session, monkeypatch)
    original = session.scalar(select(ChangeSetRevision))
    binding = deepcopy(request.selected_option_binding)
    authority = request.authority_scope_hash
    original_id, parameter_hash = original.id, original.parameter_hash
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    adopted = _adopt(session, utterance, trace, 2)
    assert adopted["readiness"] == "ready_to_commit"
    assert adopted["observation_required"] is True
    assert adopted["current_revision"] == 2
    assert request.selected_option_binding == binding
    assert request.authority_scope_hash == authority
    assert request.authority_availability == "available"
    proof = session.get(RequestAssemblyAdoption, request.ref_id)
    assert proof.original_revision_id == original_id
    assert proof.proof_json["original_parameter_hash"] == parameter_hash
    assert proof.proof_json["origin_utterances"][0]["utterance_ref"] == utterance.ref_id
    assert session.scalar(select(func.count(Item.id))) == 0
    conflict = _commit_adoption(session, utterance, trace, 3)
    assert conflict["disposition"] == "draft_revision_conflict"
    _review_adoption(session, utterance, trace, 4)
    duplicate = _adopt(session, utterance, trace, 5)
    assert duplicate["disposition"] == "no_op"
    assert changeset.current_revision == 2
    session.commit()
    session.expire_all()
    receipt = _commit_adoption(session, utterance, trace, 6)
    assert receipt["canonical_disposition"] == "committed"
    assert session.scalar(select(func.count(Item.id))) == 1
    assert session.scalar(select(Item)).title == "Tracked request"
    assert request.authority_availability == "consumed_committed"
    assert request.selected_option_binding == binding
    assert session.get(ChangeSetRevision, original_id).parameter_hash == parameter_hash
    # The original operation's replay does not adopt/commit another revision.
    replay = _adopt(session, utterance, trace, 2)
    assert replay == {**adopted, "replayed": True}
    assert _commit_adoption(session, utterance, new_public_ref("trace"), 1) == receipt
    assert session.scalar(select(func.count(RequestAssemblyAdoption.semantic_request_ref))) == 1
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 2
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.event_type == "changeset.request_adopted",
    )) == 1


@pytest.mark.parametrize("change", ["title", "extra", "remove"])
def test_adopted_draft_repair_cannot_expand_or_discard_original_scope(session, monkeypatch, change):
    utterance, request, changeset = _failed_item_request(session, monkeypatch)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    _review_adoption(session, utterance, trace, 3)
    payload = _item_stage(utterance).model_dump(mode="json")
    payload["assembly_scope"] = None
    operation = payload["patch"]["operations"][0]
    if change == "title":
        operation["action"]["create_spec"]["title"] = "An unrelated request"
    elif change == "extra":
        extra = deepcopy(operation)
        extra["action"]["change_id"] = "extra"
        extra["action"]["create_spec"]["title"] = "Unrequested second Item"
        payload["patch"]["operations"].append(extra)
    else:
        payload["patch"]["operations"] = [{
            "operation": "action_remove", "change_id": "tracked-request",
        }]
    before = deepcopy(changeset.staged_actions_json)
    token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=4,
        call_id="not-a-repair", tool_name="docket_stage_changes", argument_hash="a" * 64,
    )
    with pytest.raises(DocketError) as error, session.begin_nested():
        ChangeSetAssemblyService(session).stage(
            StageChangesInput.model_validate(payload), assembly_operation_token=token,
            assembly_argument_hash="a" * 64,
        )
    assert error.value.code == "adopted_request_scope_conflict"
    assert changeset.staged_actions_json == before
    assert changeset.current_revision == 2
    assert request.authority_availability == "available"
    assert session.scalar(select(func.count(Item.id))) == 0


@pytest.mark.parametrize("corruption", [
    "scope_format", "scope_missing", "scope_hash", "snapshot", "compiler_manifest", "staged_inputs",
])
def test_unprovable_adoption_preserves_original_authority_and_draft(
    session, monkeypatch, corruption,
):
    utterance, request, changeset = _failed_item_request(session, monkeypatch)
    if corruption == "scope_format":
        request.selected_option_binding = {
            **request.selected_option_binding, "scope": {"old_scope": True},
        }
    elif corruption == "scope_missing":
        request.selected_option_binding = {**request.selected_option_binding, "scope": None}
    elif corruption == "scope_hash":
        request.authority_scope_hash = "0" * 64
    elif corruption == "compiler_manifest":
        changeset.compiler_manifest_json = {"unverified": True}
    elif corruption == "staged_inputs":
        changeset.staged_actions_json = []
    else:
        changeset.basis_refs = []
    session.flush()
    original_binding = deepcopy(request.selected_option_binding)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    with pytest.raises(DocketError) as error, session.begin_nested():
        _adopt(session, utterance, trace, 2)
    assert error.value.code == "request_adoption_unproven"
    assert error.value.details["new_authorization_required"] is False
    assert error.value.details["category"] == "implementation_validation"
    assert request.selected_option_binding == original_binding
    assert request.authority_availability == "available"
    assert changeset.current_revision == 1
    assert session.get(RequestAssemblyAdoption, request.ref_id) is None
    assert session.scalar(select(func.count(Item.id))) == 0


@pytest.mark.parametrize("explicit_binding", [False, True])
def test_adopted_request_cannot_reenter_old_direct_service_path(
    session, monkeypatch, explicit_binding,
):
    utterance, request, changeset = _failed_item_request(session, monkeypatch)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    options = {}
    if explicit_binding:
        options = {
            "semantic_request_ref": request.ref_id,
            "authority_scope_hash": request.authority_scope_hash,
            "precondition_hash": request.current_precondition_hash,
        }
    result = InteractiveAuthorityService(session).process_turn(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        actor_id=get_settings().operator_discord_user_id,
        intent_session_ref=request.intent_session_ref, expected_session_version=None,
        statements=[], relations=[], resolved_intent_json={"kind": "fixture"},
        blocking_clarifications=[],
        content=ChangeSetContent(
            basis_refs=[utterance.ref_id],
            tracked_context_changes=[_item_stage(utterance).patch.operations[0].action],
        ),
        changeset_ref=changeset.ref_id, expected_changeset_version=changeset.version, **options,
    )
    assert result["disposition"] == "assembled_draft_exists"
    assert changeset.state == "validated"
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(IntentSession)).semantic_state == "ready"


def test_adoption_requires_exact_observation_and_reconciles_another_attempt(session, monkeypatch):
    utterance, _request, changeset = _failed_item_request(session, monkeypatch)
    trace_a, trace_b = new_public_ref("trace"), new_public_ref("trace")
    not_observed = _adopt(session, utterance, trace_a, 1)
    assert not_observed["disposition"] == "draft_revision_conflict"
    _review_adoption(session, utterance, trace_a, 2)
    _review_adoption(session, utterance, trace_b, 1)
    first = _adopt(session, utterance, trace_a, 3)
    assert first["disposition"] == "ready_to_commit"
    stale = _adopt(session, utterance, trace_b, 2)
    assert stale["disposition"] == "draft_revision_conflict"
    _review_adoption(session, utterance, trace_b, 3)
    reconciled = _adopt(session, utterance, trace_b, 4)
    assert reconciled["disposition"] == "no_op"
    assert changeset.current_revision == 2


def test_adopted_compiler_failure_is_repairable_without_authority_loss(session, monkeypatch):
    utterance, request, changeset = _failed_item_request(session, monkeypatch)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    _review_adoption(session, utterance, trace, 3)
    payload = _item_stage(utterance).model_copy(update={"assembly_scope": None})
    service = ChangeSetAssemblyService(session)

    def compiler_error(*args, **kwargs):
        raise DocketError(
            code="fixture_compiler_failure", message="Synthetic runtime compiler failure.",
            details={"category": "implementation_validation"},
        )

    with monkeypatch.context() as patch:
        patch.setattr(service.changesets, "_compile_required_provider_intents", compiler_error)
        token = _admit(
            session, utterance=utterance, trace_ref=trace, ordinal=4,
            call_id="compiler-failed", tool_name="docket_stage_changes", argument_hash="e" * 64,
        )
        failed = service.stage(
            payload, assembly_operation_token=token, assembly_argument_hash="e" * 64,
        )
    assert failed["disposition"] == "saved_with_errors"
    assert len(changeset.staged_actions_json) == 1
    assert request.authority_availability == "available"
    assert session.scalar(select(IntentSession)).semantic_state == "ready"
    assert _commit_adoption(session, utterance, trace, 5)["disposition"] == "rejected_validation"
    new_token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=6,
        call_id="compiler-repaired", tool_name="docket_stage_changes", argument_hash="e" * 64,
    )
    repaired = service.stage(
        payload, assembly_operation_token=new_token, assembly_argument_hash="e" * 64,
    )
    assert repaired["disposition"] == "ready_to_commit"
    assert service.stage(
        payload, assembly_operation_token=token, assembly_argument_hash="e" * 64,
    ) == {**failed, "replayed": True}
    assert _commit_adoption(session, utterance, trace, 7)["disposition"] == "committed"
    assert session.scalar(select(func.count(Item.id))) == 1


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_adoption_proof_is_immutable(session, monkeypatch, mutation):
    utterance, request, _changeset = _failed_item_request(session, monkeypatch)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    session.commit()
    proof = session.get(RequestAssemblyAdoption, request.ref_id)
    if mutation == "update":
        proof.proof_hash = "a" * 64
    else:
        session.delete(proof)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()


def test_shared_commit_guard_rejects_revised_adopted_effects(session, monkeypatch):
    from docket.schemas.authority import ChangeSetCommit

    utterance, _request, changeset = _failed_item_request(session, monkeypatch)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    service = InteractiveAuthorityService(session).changesets
    # Simulate an obsolete internal caller producing a new well-pinned revision.
    # The pin alone is not proof of equivalence to the original authorization.
    content = service._content(changeset)
    action = content.tracked_context_changes[0]
    action.create_spec.title = "Not the original request"
    changeset.current_revision += 1
    changeset.version += 1
    service._sync_snapshot(changeset, content)
    service._revision(changeset, content, changeset.current_revision)
    session.flush()
    with pytest.raises(DocketError) as error:
        service.commit(ChangeSetCommit(
            changeset_ref=changeset.ref_id, expected_version=changeset.version,
            idempotency_key=changeset.idempotency_key, authority_utterance_ref=utterance.ref_id,
        ))
    assert error.value.code == "adopted_request_scope_conflict"
    assert session.scalar(select(func.count(Item.id))) == 0


def test_adoption_source_identity_survives_and_changed_evidence_blocks_commit(session, monkeypatch):
    utterance, request, _changeset = _failed_item_request(session, monkeypatch, with_source=True)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    _adopt(session, utterance, trace, 2)
    _review_adoption(session, utterance, trace, 3)
    source = session.scalar(select(Source))
    proof = session.get(RequestAssemblyAdoption, request.ref_id)
    assert proof.proof_json["source_bindings"] == [{
        "source_ref": source.ref_id, "source_manifest_hash": source.content_hash,
        "attachment_content_hash": None, "evidence_state": "source_recorded",
    }]
    # SQL deliberately bypasses the SQLite ORM guard, not the production trigger.
    session.execute(Source.__table__.update().values(content_hash="0" * 64))
    session.expire_all()
    with pytest.raises(DocketError) as error:
        _commit_adoption(session, utterance, trace, 4)
    assert error.value.code == "request_adoption_unproven"
    assert error.value.details["constraint"] == "immutable_adoption_evidence"
    assert request.authority_availability == "available"
    assert session.scalar(select(func.count(Item.id))) == 0


def test_adopted_calendar_keeps_provider_identity_through_recompile_and_commit(
    session, monkeypatch,
):
    utterance = _utterance("1542799000000000884")
    session.add(utterance)
    session.flush()
    account = ProviderAccount(
        provider="google", external_account_id="calendar-adoption-fixture",
        capabilities=["google_calendar"], enabled=True,
    )
    session.add(account)
    session.flush()
    lane = CalendarLane(
        account_id=account.id, lane="adoption", display_name="Meetings",
        calendar_id="adoption@example.com", color_hex="#3367D6", status="active",
        basis_refs=[utterance.ref_id], created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(lane)
    session.flush()
    content = ChangeSetContent.model_validate({
        "basis_refs": [utterance.ref_id],
        "lane_changes": [{
            "mutation_type": "lane_routing_decision_create", "change_id": "route",
            "action": "create", "object_type": "lane_routing_decision",
            "basis_refs": [utterance.ref_id], "affected_fields": ["lane"],
            "create_spec": {
                "lane_ref": lane.ref_id, "event_change_id": "meeting",
                "decision_kind": "explicit_operator", "operator_confirmed": True,
            },
        }],
        "event_changes": [{
            "mutation_type": "canonical_event_create", "change_id": "meeting",
            "action": "create", "object_type": "canonical_event",
            "basis_refs": [utterance.ref_id], "affected_fields": ["event", "lane"],
            "create_spec": {
                "title": "Preserved meeting", "lane_ref": lane.ref_id,
                "event_spec": {
                    "title": "Preserved meeting", "calendar_lane": lane.lane,
                    "location": "Room 121", "timing": {
                        "kind": "timed", "start_local": "2026-09-16T10:00:00",
                        "end_local": "2026-09-16T11:00:00", "timezone": "America/Los_Angeles",
                    },
                },
            },
        }],
    })
    utterance, request, changeset = _retain_failed_request(session, monkeypatch, utterance, content)
    assert len(changeset.provider_intents) == 1
    provider_identity = deepcopy(changeset.provider_intents)
    trace = new_public_ref("trace")
    _review_adoption(session, utterance, trace, 1)
    adopted = _adopt(session, utterance, trace, 2)
    assert adopted["disposition"] == "ready_to_commit", adopted["diagnostic_sample"]
    _review_adoption(session, utterance, trace, 3)
    payload = StageChangesInput(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        patch={"operations": [{"operation": "draft_recompile"}]},
    )
    digest = sha256_json(payload.model_dump(mode="json"))
    token = _admit(
        session, utterance=utterance, trace_ref=trace, ordinal=4,
        call_id="adopted-recompile", tool_name="docket_stage_changes", argument_hash=digest,
    )
    migrated = ChangeSetAssemblyService(session).stage(
        payload, assembly_operation_token=token, assembly_argument_hash=digest,
    )
    assert migrated["disposition"] == "ready_to_commit"
    assert changeset.provider_intents == provider_identity
    _review_adoption(session, utterance, trace, 5)
    receipt = _commit_adoption(session, utterance, trace, 6)
    assert receipt["canonical_disposition"] == "committed"
    assert receipt["provider_disposition"] == "queued"
    assert receipt["provider_operation_count"] == 1
    assert session.scalar(select(func.count(Operation.id))) == 1
    event = session.scalar(select(CanonicalEvent))
    assert event.title == "Preserved meeting"
    assert event.lane_ref == lane.ref_id
    assert event.event_spec["location"] == "Room 121"
    assert request.authority_availability == "consumed_committed"

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.mcp.server import docket_stage_changes, mcp
from docket.models import (
    AssemblyOperation,
    AttachmentEvidence,
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    Item,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    SemanticRequest,
    SemanticRequestAttempt,
    Source,
    Task,
    TemporalBinding,
    ToolInvocation,
)
from docket.schemas.assembly import (
    AssemblyAuthorityScopeInput,
    ReviewChangesInput,
    StageChangesInput,
)
from docket.schemas.authority import ChangeSetContent
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.interactive_authority import InteractiveAuthorityService


def test_resumed_draft_executes_pinned_effects_without_current_compiler(session, monkeypatch):
    import docket.services.changeset_assembly as assembly_module
    import docket.services.changeset_pins as pins_module
    from docket.services.change_sets import ChangeSetService

    utterance = _utterance("1542799000000000891")
    source, lane = _schedule_context(session, utterance, suffix="pin-resume")
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    stage_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="pin-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    result = service.stage(
        _schedule_stage(
            utterance,
            source_ref=source.ref_id,
            lane_ref=lane.ref_id,
            start_index=0,
            count=1,
            include_scope=True,
        ),
        assembly_operation_token=stage_token,
        assembly_argument_hash="a" * 64,
    )
    assert result["disposition"] == "ready_to_commit"
    draft = session.scalar(select(ChangeSet))
    manifest = deepcopy(draft.compiler_manifest_json)
    pin = manifest["execution_pin"]
    assert pin["normalized_entry_compilers"][0]["input_schema_version"] == 2
    assert pin["normalized_entry_compilers"][0]["version"] == 2
    original_effects = deepcopy(draft.event_changes)
    original_provider_intents = deepcopy(draft.provider_intents)
    session.commit()
    session.expire_all()

    def never_compile(*args, **kwargs):
        raise AssertionError("Commit may not rerun a deployment's compiler")

    monkeypatch.setattr(assembly_module, "COMPILER_VERSION", 3)
    monkeypatch.setattr(pins_module, "COMPILER_VERSION", 2)
    monkeypatch.setattr(assembly_module, "compile_normalized_entry", never_compile)
    monkeypatch.setattr(ChangeSetService, "_compile_required_provider_intents", never_compile)
    # A fresh execution observes the immutable revision, not a regenerated draft.
    resumed_trace = new_public_ref("trace")
    review_token = _admit(
        session,
        utterance=utterance,
        trace_ref=resumed_trace,
        call_id="pin-review",
        ordinal=1,
        tool_name="docket_review_changeset",
        argument_hash="b" * 64,
    )
    service.review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
        assembly_operation_token=review_token,
        assembly_argument_hash="b" * 64,
    )
    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=resumed_trace,
        call_id="pin-commit",
        ordinal=2,
        tool_name="docket_commit_changeset",
        argument_hash="c" * 64,
    )
    receipt = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="c" * 64,
    )
    assert receipt["disposition"] == "committed"
    assert draft.compiler_manifest_json == manifest
    assert draft.event_changes == original_effects
    assert draft.provider_intents == original_provider_intents
    event = session.scalar(select(CanonicalEvent))
    assert event.title == "MATH 1263 — Topic 1"
    assert session.scalar(select(func.count(Operation.id))) == 1
    # Receipt recovery never tries to migrate/reexecute an already committed request.
    monkeypatch.setattr(pins_module, "EXECUTABLE_SCHEMA_VERSION", 2)
    replay_token = _admit(
        session,
        utterance=utterance,
        trace_ref=resumed_trace,
        call_id="pin-replay",
        ordinal=3,
        tool_name="docket_commit_changeset",
        argument_hash="d" * 64,
    )
    replay = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=replay_token,
        assembly_argument_hash="d" * 64,
    )
    assert replay == receipt
    assert session.scalar(select(func.count(Operation.id))) == 1


@pytest.mark.parametrize("drift", ["effects", "inputs", "preconditions", "binding", "manifest"])
def test_commit_rejects_mutable_snapshot_drift_from_immutable_pin(session, drift):
    utterance = _utterance("1542799000000000892")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    service.stage(
        _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="a" * 64
    )
    draft = session.scalar(select(ChangeSet))
    if drift == "effects":
        effects = deepcopy(draft.tracked_context_changes)
        effects[0]["create_spec"]["title"] = "Unobserved different title"
        draft.tracked_context_changes = effects
    elif drift == "inputs":
        draft.staged_actions_json = []
    elif drift == "preconditions":
        draft.expected_versions = {new_public_ref("item"): 4}
    elif drift == "binding":
        draft.authority_scope_hash = "f" * 64
    else:
        draft.compiler_manifest_json = {}
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="commit",
        ordinal=2,
        tool_name="docket_commit_changeset",
        argument_hash="b" * 64,
    )
    with pytest.raises(DocketError) as error:
        service.commit(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
            assembly_operation_token=token,
            assembly_argument_hash="b" * 64,
        )
    assert error.value.code == "draft_execution_pin_mismatch"
    assert error.value.details["authority_preserved"] is True
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"


def test_incompatible_execution_schema_requires_explicit_migration(session, monkeypatch):
    import docket.services.changeset_pins as pins_module

    utterance = _utterance("1542799000000000893")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    service.stage(
        _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="a" * 64
    )
    draft = session.scalar(select(ChangeSet))
    old_manifest = deepcopy(draft.compiler_manifest_json)
    monkeypatch.setattr(pins_module, "EXECUTABLE_SCHEMA_VERSION", 2)
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="commit",
        ordinal=2,
        tool_name="docket_commit_changeset",
        argument_hash="b" * 64,
    )
    with pytest.raises(DocketError) as error:
        service.commit(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
            assembly_operation_token=token,
            assembly_argument_hash="b" * 64,
        )
    assert error.value.code == "draft_migration_required"
    assert error.value.details["next_action"] == "migrate_draft"
    assert draft.compiler_manifest_json == old_manifest
    assert draft.current_revision == 1
    assert session.scalar(select(func.count(Item.id))) == 0


def test_same_patch_cannot_silently_recompile_changed_effects(session, monkeypatch):
    utterance = _utterance("1542799000000000894")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    service.stage(
        _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="a" * 64
    )
    draft = session.scalar(select(ChangeSet))
    old_manifest = deepcopy(draft.compiler_manifest_json)

    def changed_compiler(content, **kwargs):
        payload = content.model_dump(mode="json")
        payload["tracked_context_changes"][0]["create_spec"]["title"] = "Changed compiler output"
        return ChangeSetContent.model_validate(payload)

    monkeypatch.setattr(service.changesets, "_compile_required_provider_intents", changed_compiler)
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="same-patch",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="b" * 64,
    )
    with pytest.raises(DocketError) as error:
        service.stage(
            _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="b" * 64
        )
    assert error.value.code == "draft_migration_required"
    assert draft.compiler_manifest_json == old_manifest
    assert draft.current_revision == 1
    assert draft.tracked_context_changes[0]["create_spec"]["title"] == "Tracked request"
    assert session.scalar(select(func.count(Item.id))) == 0


def _recompile_request(utterance):
    return StageChangesInput(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        patch={"operations": [{"operation": "draft_recompile"}]},
    )


def test_explicit_recompile_retains_authority_and_requires_fresh_observation(session, monkeypatch):
    import docket.services.changeset_compiler as compiler
    import docket.services.changeset_pins as pins

    utterance = _utterance("1542799000000000895")
    source, lane = _schedule_context(session, utterance, suffix="explicit-recompile")
    trace = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)

    def token(name, ordinal, trace_ref=trace):
        return _admit(
            session, utterance=utterance, trace_ref=trace_ref, call_id=f"{trace_ref}-{ordinal}",
            ordinal=ordinal, tool_name=name, argument_hash="a" * 64,
        )

    service.stage(
        _schedule_stage(utterance, source_ref=source.ref_id, lane_ref=lane.ref_id,
                        start_index=0, count=3, include_scope=True),
        assembly_operation_token=token("docket_stage_changes", 1), assembly_argument_hash="a" * 64,
    )
    draft = session.scalar(select(ChangeSet))
    before = session.scalar(select(ChangeSetRevision))
    authority = draft.authority_scope_hash
    original = deepcopy(before.compiler_manifest_json)
    old_entries = deepcopy(before.normalized_entries_json)
    other = new_public_ref("trace")
    service.review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key),
        assembly_operation_token=token("docket_review_changeset", 1, other),
        assembly_argument_hash="a" * 64,
    )
    monkeypatch.setattr(pins, "EXECUTABLE_SCHEMA_VERSION", 2)
    monkeypatch.setattr(compiler, "COMPILER_VERSION", 3)
    operation_token = token("docket_stage_changes", 2)
    result = service.stage(
        _recompile_request(utterance), assembly_operation_token=operation_token,
        assembly_argument_hash="a" * 64,
    )
    assert result["disposition"] == "ready_to_commit"
    assert result["observation_required"] is True
    assert result["compiler_migration"]["semantic_scope_changed"] is False
    assert draft.current_revision == 2
    assert draft.authority_scope_hash == authority
    assert before.compiler_manifest_json == original
    assert before.normalized_entries_json == old_entries
    assert draft.compiler_manifest_json["execution_pin"]["executable_schema_version"] == 2
    assert {entry["compiler_version"] for entry in draft.normalized_entries_json} == {3}
    assert {entry["statement_ref"] for entry in old_entries} == {
        entry["statement_ref"] for entry in draft.normalized_entries_json
    }
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.event_type == "changeset.recompiled"
    )) == 1
    assert {attempt.observed_draft_revision for attempt in session.scalars(
        select(SemanticRequestAttempt)
    )} == {1}

    for execution, ordinal in ((trace, 3), (other, 2)):
        blocked = service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            assembly_operation_token=token("docket_commit_changeset", ordinal, execution),
            assembly_argument_hash="a" * 64,
        )
        assert blocked["disposition"] == "draft_revision_conflict"
    diff = service.review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                           **result["diff_review"]),
        assembly_operation_token=token("docket_review_changeset", 4),
        assembly_argument_hash="a" * 64,
    )
    assert any(row["subject_kind"] == "compiler_pin" for row in diff["items"])
    # A receipt-bound first page observes its exact still-current revision,
    # without making the other attempt aware of that revision.
    assert {attempt.observed_draft_revision for attempt in session.scalars(
        select(SemanticRequestAttempt)
    )} == {1, 2}
    receipt = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=token("docket_commit_changeset", 5),
        assembly_argument_hash="a" * 64,
    )
    assert receipt["disposition"] == "committed"
    assert set(session.scalars(select(CanonicalEvent.title))) == {
        "MATH 1263 — Topic 1", "MATH 1263 — Topic 2", "MATH 1263 — Topic 3",
    }
    assert session.scalar(select(func.count(Operation.id))) == 3
    replay = service.stage(
        _recompile_request(utterance), assembly_operation_token=operation_token,
        assembly_argument_hash="a" * 64,
    )
    assert replay["replayed"] is True
    assert replay["compiler_migration"] == result["compiler_migration"]
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 2
    assert session.scalar(select(func.count(ChangeSet.id))) == 1


@pytest.mark.parametrize("change", ["title", "additional_effect", "removed_all"])
def test_recompile_rejects_changed_meaning_without_replacing_draft(session, monkeypatch, change):
    utterance = _utterance("1542799000000000896")
    session.add(utterance)
    session.flush()
    trace = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    stage_token = _admit(session, utterance=utterance, trace_ref=trace, call_id="stage", ordinal=1,
                         tool_name="docket_stage_changes", argument_hash="a" * 64)
    service.stage(_item_stage(utterance), assembly_operation_token=stage_token,
                  assembly_argument_hash="a" * 64)
    draft = session.scalar(select(ChangeSet))
    old = deepcopy(draft.compiler_manifest_json)

    def bad_compiler(content, **_kwargs):
        payload = content.model_dump(mode="json")
        item = payload["tracked_context_changes"][0]
        if change == "title":
            item["create_spec"]["title"] = "Unrelated work"
        else:
            extra = deepcopy(item)
            extra["change_id"] = "extra-unrequested-item"
            payload["tracked_context_changes"].append(extra)
        return ChangeSetContent.model_validate(payload)

    monkeypatch.setattr(service.changesets, "_compile_required_provider_intents", bad_compiler)
    if change == "removed_all":
        monkeypatch.setattr(service, "_content", lambda **_kwargs: None)
    recompile_token = _admit(session, utterance=utterance, trace_ref=trace, call_id="recompile",
                            ordinal=2, tool_name="docket_stage_changes", argument_hash="b" * 64)
    with pytest.raises(DocketError) as failure, session.begin_nested():
        service.stage(_recompile_request(utterance), assembly_operation_token=recompile_token,
                      assembly_argument_hash="b" * 64)
    assert failure.value.code == "draft_recompile_semantic_conflict"
    assert failure.value.details["difference_count"] > 0
    assert draft.current_revision == 1
    assert draft.compiler_manifest_json == old
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"
    assert session.scalar(select(func.count(Item.id))) == 0


@pytest.mark.parametrize("change", ["event_date", "provider_account", "fourth_event"])
def test_calendar_recompile_cannot_expand_pinned_effects(session, monkeypatch, change):
    utterance = _utterance("1542799000000000899")
    source, lane = _schedule_context(session, utterance, suffix="recompile-calendar-boundary")
    other_account = ProviderAccount(provider="google", external_account_id="unrequested-account",
                                    capabilities=["google_calendar"], enabled=True)
    session.add(other_account)
    session.flush()
    trace = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    token = _admit(session, utterance=utterance, trace_ref=trace, call_id="stage", ordinal=1,
                   tool_name="docket_stage_changes", argument_hash="a" * 64)
    service.stage(_schedule_stage(utterance, source_ref=source.ref_id, lane_ref=lane.ref_id,
                                  start_index=0, count=3, include_scope=True),
                  assembly_operation_token=token, assembly_argument_hash="a" * 64)
    draft = session.scalar(select(ChangeSet))
    original_effects = deepcopy(draft.event_changes)
    compile_original = service.changesets._compile_required_provider_intents

    def bad_compiler(content, **kwargs):
        payload = compile_original(content, **kwargs).model_dump(mode="json")
        event = payload["event_changes"][0]
        if change == "event_date":
            timing = event["create_spec"]["event_spec"]["timing"]
            timing["start_local"] = "2026-12-01T09:00:00"
            timing["end_local"] = "2026-12-01T09:50:00"
        elif change == "provider_account":
            payload["provider_intents"][0]["account_ref"] = other_account.ref_id
        else:
            extra = deepcopy(event)
            extra["change_id"] = "fourth-unrequested-event"
            extra["create_spec"]["canonical_key"] = "unrequested-calendar-entry"
            payload["event_changes"].append(extra)
        return ChangeSetContent.model_validate(payload)

    monkeypatch.setattr(service.changesets, "_compile_required_provider_intents", bad_compiler)
    token = _admit(session, utterance=utterance, trace_ref=trace, call_id="recompile", ordinal=2,
                   tool_name="docket_stage_changes", argument_hash="b" * 64)
    with pytest.raises(DocketError) as failure, session.begin_nested():
        service.stage(_recompile_request(utterance), assembly_operation_token=token,
                      assembly_argument_hash="b" * 64)
    assert failure.value.code == "draft_recompile_semantic_conflict"
    assert draft.current_revision == 1
    assert draft.event_changes == original_effects
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0


def test_recompile_cannot_mix_input_or_authority_changes(session):
    utterance = _utterance("1542799000000000897")
    session.add(utterance)
    session.flush()
    payload = _recompile_request(utterance).model_dump(mode="json", exclude_none=True)
    for extra in (
        {"assembly_scope": _scope().model_dump(mode="json")},
        {"expected_versions": {new_public_ref("item"): 2}},
        {"patch": {"operations": [
            {"operation": "draft_recompile"}, {"operation": "action_remove", "change_id": "one"},
        ]}},
    ):
        with pytest.raises(ValidationError):
            StageChangesInput.model_validate({**payload, **extra})


def test_mcp_recompile_cross_field_rejection_terminalizes_admission(session_factory):
    from docket.mcp.instrumented import _result_envelope

    trace = new_public_ref("trace")
    with session_factory.begin() as session:
        utterance = _utterance("1542799000000000898")
        session.add(utterance)
        session.flush()
        initial = _admit(session, utterance=utterance, trace_ref=trace, call_id="stage", ordinal=1,
                         tool_name="docket_stage_changes", argument_hash="a" * 64)
        ChangeSetAssemblyService(session).stage(_item_stage(utterance),
                                               assembly_operation_token=initial,
                                               assembly_argument_hash="a" * 64)
    for ordinal, forbidden_scope in ((2, True), (3, False)):
        arguments = {"patch": {"operations": [{"operation": "draft_recompile"}]}}
        if forbidden_scope:
            arguments["assembly_scope"] = _scope().model_dump(mode="json", exclude_none=True)
        digest = sha256_json(arguments)
        with session_factory.begin() as session:
            token = _admit(session, utterance=utterance, trace_ref=trace,
                           call_id=f"recompile-{ordinal}", ordinal=ordinal,
                           tool_name="docket_stage_changes", argument_hash=digest)
        result = asyncio.run(mcp.call_tool("docket_stage_changes", {
            **arguments, "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
            "assembly_operation_token": token, "assembly_argument_hash": digest,
        }))
        envelope = _result_envelope(result)
        if forbidden_scope:
            assert envelope["disposition"] == "rejected_validation"
            assert envelope["error"]["code"] == "validation_error"
        else:
            assert envelope["disposition"] == "ready_to_commit"
            assert envelope["observation_required"] is True
        with session_factory() as session:
            operation = session.scalar(select(AssemblyOperation).where(
                AssemblyOperation.operation_key == token
            ))
            assert operation.state == ("rejected" if forbidden_scope else "completed")
            assert session.scalar(select(ChangeSet.current_revision)) == (
                1 if forbidden_scope else 2
            )
            assert all(row.transport_state == "completed"
                       for row in session.scalars(select(ToolInvocation)))
            assert session.scalar(select(func.count(Item.id))) == 0


@pytest.mark.parametrize("binding", ["exact", "other_message", "other_hash", "dispatched"])
def test_local_admission_loss_recovers_only_exact_undispatched_predecessor(session, binding):
    from docket.internal_api.schemas import McpTraceCallUpdate, McpTraceUpdate
    from docket.services.mcp_traces import McpTraceService
    from docket.tool_contracts import CONTRACT_VERSION, contract_hash

    settings = get_settings()
    utterance = _utterance("1542799000000000880")
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    first = _admit(session, utterance=utterance, trace_ref=trace_ref, call_id="lost-admission",
                   ordinal=1, tool_name="docket_stage_changes", argument_hash="a" * 64)
    # Admission committed but its response was lost. The gateway did not invoke MCP.
    for state in ("running", "completed"):
        McpTraceService(session).update(trace_ref, McpTraceUpdate(
            request_id="00000000-0000-0000-0000-000000000001",
            guild_id=settings.discord_guild_id, source_channel_id=settings.chat_channel_id,
            source_message_id=("1542799000000000881" if binding == "other_message"
                               else "1542799000000000880"),
            actor_id=settings.operator_discord_user_id, tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"), caller_profile="interactive",
            turn_started_at=utterance.said_at, updated_at=datetime.now(UTC),
            call=McpTraceCallUpdate(
                call_id="lost-admission", ordinal=1, tool_name="docket_stage_changes",
                execution_boundary=(
                    "mcp_attempted" if binding == "dispatched" else "local_rejection"
                ),
                transport_state=state,
                disposition="failed" if state == "completed" else None,
                received_argument_hash="b" * 64 if binding == "other_hash" else "a" * 64,
            ),
        ))
    second = _admit(session, utterance=utterance, trace_ref=trace_ref, call_id="corrected-new-call",
                    ordinal=2, tool_name="docket_stage_changes", argument_hash="c" * 64)
    service = ChangeSetAssemblyService(session)
    if binding == "exact":
        result = service.stage(_item_stage(utterance), assembly_operation_token=second,
                               assembly_argument_hash="c" * 64)
        assert result["disposition"] == "ready_to_commit"
    else:
        with pytest.raises(DocketError) as failure:
            service.stage(_item_stage(utterance), assembly_operation_token=second,
                          assembly_argument_hash="c" * 64)
        assert failure.value.code == "assembly_operation_out_of_order"
    predecessor = session.scalar(select(AssemblyOperation).where(
        AssemblyOperation.operation_key == first
    ))
    assert predecessor.state == ("rejected" if binding == "exact" else "admitted")
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(func.count(ToolInvocation.id))) == 0


def _utterance(message_id: str) -> OperatorUtterance:
    settings = get_settings()
    text = "Track this item and its follow-up task."
    return OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=(
            f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}"
        ),
        said_at=datetime.now(UTC),
        verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
    )


def _admit(
    session,
    *,
    utterance: OperatorUtterance,
    trace_ref: str,
    call_id: str,
    ordinal: int,
    tool_name: str,
    argument_hash: str,
) -> str:
    settings = get_settings()
    result = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=trace_ref,
        upstream_tool_call_id=call_id,
        trace_ordinal=ordinal,
        tool_name=tool_name,
        argument_hash=argument_hash,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        source_message_id=utterance.request_key.split(":")[3],
        actor_id=settings.operator_discord_user_id,
    )
    return str(result["assembly_operation_token"])


def _scope() -> AssemblyAuthorityScopeInput:
    return AssemblyAuthorityScopeInput(
        resolved_intent={"intent": "track item and follow-up"},
        allowed_mutation_types=["item_create", "task_create"],
        planned_create_types=["item", "task"],
    )


def _item_stage(utterance: OperatorUtterance) -> StageChangesInput:
    return StageChangesInput.model_validate(
        {
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "assembly_scope": _scope().model_dump(mode="json"),
            "patch": {
                "operations": [
                    {
                        "operation": "action_upsert",
                        "action": {
                            "mutation_type": "item_create",
                            "change_id": "tracked-request",
                            "action": "create",
                            "object_type": "item",
                            "affected_fields": ["title", "kind"],
                            "basis_refs": [utterance.ref_id],
                            "create_spec": {
                                "title": "Tracked request",
                                "kind": "test.request",
                            },
                        },
                    }
                ]
            },
        }
    )


def _task_stage(utterance: OperatorUtterance) -> StageChangesInput:
    return StageChangesInput.model_validate(
        {
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "patch": {
                "operations": [
                    {
                        "operation": "action_upsert",
                        "action": {
                            "mutation_type": "task_create",
                            "change_id": "follow-up",
                            "action": "create",
                            "object_type": "task",
                            "affected_fields": ["item_ref", "task_state"],
                            "basis_refs": [utterance.ref_id],
                            "create_spec": {
                                "item_change_id": "tracked-request",
                                "title": "Follow up",
                            },
                        },
                    }
                ]
            },
        }
    )


def _schedule_stage(
    utterance: OperatorUtterance,
    *,
    source_ref: str,
    lane_ref: str,
    start_index: int,
    count: int,
    include_scope: bool,
    selected_count: int | None = None,
) -> StageChangesInput:
    base = datetime(2026, 9, 8, 9, 0)
    operations = []
    for index in range(start_index, start_index + count):
        start = base + timedelta(days=index)
        end = start + timedelta(minutes=50)
        operations.append(
            {
                "operation": "normalized_entry_upsert",
                "entry": {
                    "entry_type": "scheduled_occurrence_entry",
                    "import_entry_id": f"math-1263-entry-{index:02d}",
                    "evidence": {
                        "source_ref": source_ref,
                        "source_fragment_locator": {"page": 1, "cell": index},
                        "source_fragment_hash": hashlib.sha256(
                            f"matrix-cell-{index}".encode()
                        ).hexdigest(),
                        "extractor_identifier": "docket.attachment-text",
                        "extractor_version": "1",
                    },
                    "title": f"MATH 1263 — Topic {index + 1}",
                    "kind": "academic.lecture_topic",
                    "lane_ref": lane_ref,
                    "timing": {
                        "kind": "timed",
                        "start_local": start.isoformat(),
                        "end_local": end.isoformat(),
                        "timezone": "America/Los_Angeles",
                    },
                },
            }
        )
    scope = None
    if include_scope:
        scope = {
            "resolved_intent": {
                "intent": "replace course schedule", "entry_count": selected_count or count,
            },
            "normalized_entry_types": ["scheduled_occurrence_entry", "schedule_exception_entry"],
            "selected_entry_ids": [
                f"math-1263-entry-{index:02d}" for index in range(selected_count or count)
            ],
            "target_refs": [lane_ref],
            "source_refs": [source_ref],
            "explicit_exclusions": ["generic recurrence"],
        }
    return StageChangesInput.model_validate(
        {
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "assembly_scope": scope,
            "patch": {"operations": operations},
        }
    )


def _schedule_context(
    session,
    utterance: OperatorUtterance,
    *,
    suffix: str,
) -> tuple[Source, CalendarLane]:
    source = Source(
        source_kind="attachment",
        external_ref=f"discord-attachment:{suffix}",
        observed_at=datetime.now(UTC),
        content_hash=hashlib.sha256(suffix.encode()).hexdigest(),
        metadata_json={},
    )
    account = ProviderAccount(
        provider="google",
        external_account_id=f"assembly-{suffix}",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add_all([source, account])
    session.flush()
    utterance.attachment_source_refs = [source.ref_id]
    session.add(utterance)
    session.flush()
    evidence = AttachmentEvidence(
        ref_id=source.ref_id,
        transport="discord",
        transport_attachment_ref=suffix,
        source_message_ref=utterance.source_message_ref,
        operator_utterance_ref=utterance.ref_id,
        filename=f"{suffix}.png",
        media_type="image/png",
        byte_size=4096,
        content_hash=source.content_hash,
        received_at=utterance.said_at,
        ingest_state="available",
        retention_disposition="derived_only",
    )
    lane = CalendarLane(
        account_id=account.id,
        lane="math-1263",
        display_name="MATH 1263",
        color_hex="#3367D6",
        calendar_id=f"{suffix}@example.com",
        status="active",
        basis_refs=[utterance.ref_id],
        created_by_changeset_ref="chg_01M1A100000000000000000000",
    )
    session.add_all([evidence, lane])
    session.flush()
    return source, lane


@pytest.mark.integration
def test_incremental_staging_reviews_and_commits_once(session) -> None:
    message_id = "1542799000000000801"
    utterance = _utterance(message_id)
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    argument_hash = "a" * 64

    first_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage-1",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash=argument_hash,
    )
    service = ChangeSetAssemblyService(session)
    first = service.stage(
        _item_stage(utterance),
        assembly_operation_token=first_token,
        assembly_argument_hash=argument_hash,
    )
    assert first["disposition"] == "ready_to_commit"
    assert first["current_revision"] == 1

    replay = service.stage(
        _item_stage(utterance),
        assembly_operation_token=first_token,
        assembly_argument_hash=argument_hash,
    )
    assert replay["replayed"] is True
    assert replay["current_revision"] == 1

    second_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage-2",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="b" * 64,
    )
    second = service.stage(
        _task_stage(utterance),
        assembly_operation_token=second_token,
        assembly_argument_hash="b" * 64,
    )
    assert second["disposition"] == "ready_to_commit"
    assert second["current_revision"] == 2
    assert second["totals"] == {"item": 1, "task": 1}

    review_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="review-1",
        ordinal=3,
        tool_name="docket_review_changeset",
        argument_hash="c" * 64,
    )
    review = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
            view="actions",
        ),
        assembly_operation_token=review_token,
        assembly_argument_hash="c" * 64,
    )
    assert review["disposition"] == "reviewed"
    assert review["revision"] == 2
    assert review["count"] == 2

    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="commit-1",
        ordinal=4,
        tool_name="docket_commit_changeset",
        argument_hash="d" * 64,
    )
    committed = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="d" * 64,
    )
    assert committed["ok"] is True
    assert committed["disposition"] == "committed", committed
    assert committed["canonical_effect_count"] == 2
    assert committed["provider_disposition"] == "no_provider_operations"
    assert session.scalar(select(func.count(Item.id))) == 1
    assert session.scalar(select(func.count(Task.id))) == 1
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None
    assert changeset.commit_receipt_json == committed

    lost_response_replay = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="d" * 64,
    )
    assert lost_response_replay["replayed"] is True
    assert lost_response_replay["changeset_ref"] == committed["changeset_ref"]

    later_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage-after-commit",
        ordinal=5,
        tool_name="docket_stage_changes",
        argument_hash="e" * 64,
    )
    terminal_stage = service.stage(
        _item_stage(utterance),
        assembly_operation_token=later_token,
        assembly_argument_hash="e" * 64,
    )
    assert terminal_stage["disposition"] == "already_committed"
    assert session.scalar(select(func.count(ChangeSet.id))) == 1
    assert session.scalar(select(func.count(Item.id))) == 1
    assert session.scalar(select(func.count(Task.id))) == 1


@pytest.mark.integration
def test_concurrently_admitted_stale_stage_cannot_overwrite_newer_revision(session) -> None:
    utterance = _utterance("1542799000000000802")
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    first_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="concurrent-1",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    stale_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="concurrent-2",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="2" * 64,
    )
    service = ChangeSetAssemblyService(session)
    assert (
        service.stage(
            _item_stage(utterance),
            assembly_operation_token=first_token,
            assembly_argument_hash="1" * 64,
        )["disposition"]
        == "ready_to_commit"
    )
    conflict = service.stage(
        _task_stage(utterance),
        assembly_operation_token=stale_token,
        assembly_argument_hash="2" * 64,
    )
    assert conflict["disposition"] == "draft_revision_conflict"
    assert conflict["error"]["details"] == {
        "observed_revision": None,
        "current_revision": 1,
    }
    assert session.scalar(select(func.count(AssemblyOperation.id))) == 2
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None
    assert changeset.current_revision == 1


@pytest.mark.integration
def test_thirty_entry_schedule_stages_in_batches_and_commits_once(session) -> None:
    utterance = _utterance("1542799000000000803")
    source, lane = _schedule_context(
        session,
        utterance,
        suffix="math-1263-matrix",
    )

    trace_ref = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    for batch_index, start_index in enumerate((0, 15), start=1):
        argument_hash = str(batch_index) * 64
        token = _admit(
            session,
            utterance=utterance,
            trace_ref=trace_ref,
            call_id=f"schedule-stage-{batch_index}",
            ordinal=batch_index,
            tool_name="docket_stage_changes",
            argument_hash=argument_hash,
        )
        result = service.stage(
            _schedule_stage(
                utterance,
                source_ref=source.ref_id,
                lane_ref=lane.ref_id,
                start_index=start_index,
                count=15,
                include_scope=batch_index == 1,
                selected_count=30,
            ),
            assembly_operation_token=token,
            assembly_argument_hash=argument_hash,
        )
        assert result["disposition"] == (
            "saved_with_errors" if batch_index == 1 else "ready_to_commit"
        )
        assert len(json.dumps(result, separators=(",", ":")).encode()) < 16 * 1024

    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="schedule-commit",
        ordinal=3,
        tool_name="docket_commit_changeset",
        argument_hash="f" * 64,
    )
    committed = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="f" * 64,
    )
    assert committed["disposition"] == "committed", committed
    assert committed["canonical_effect_count"] == 120
    assert committed["provider_disposition"] == "queued"
    assert committed["provider_operation_count"] == 30
    assert len(json.dumps(committed, separators=(",", ":")).encode()) < 16 * 1024
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 30
    assert session.scalar(select(func.count(Operation.id))) == 30
    assert session.scalar(select(func.count(ChangeSet.id))) == 1


@pytest.mark.integration
def test_three_career_fair_entries_compile_once_without_review(session) -> None:
    utterance = _utterance("1542799000000000840")
    utterance.verbatim_text = "Add these three career fair occurrences to my Meetings lane."
    utterance.content_hash = hashlib.sha256(utterance.verbatim_text.encode()).hexdigest()
    source, lane = _schedule_context(session, utterance, suffix="career-fair")
    lane.lane = "meetings"
    lane.display_name = "Meetings"
    session.flush()
    request = _schedule_stage(
        utterance,
        source_ref=source.ref_id,
        lane_ref=lane.ref_id,
        start_index=0,
        count=3,
        include_scope=True,
    ).model_dump(mode="json", exclude_none=True)
    request["assembly_scope"]["resolved_intent"] = {
        "intent": "add the three source career-fair occurrences",
        "entry_count": 3,
    }
    request["assembly_scope"]["normalized_entry_types"] = ["scheduled_occurrence_entry"]
    expected = []
    for index, operation in enumerate(request["patch"]["operations"]):
        entry = operation["entry"]
        entry["import_entry_id"] = f"career-fair-{index + 1}"
        entry["title"] = "2026 Fall Career Fair" if index < 2 else "2026 Business Career Fair"
        entry["kind"] = "career.fair"
        entry["location"] = "Cal Poly Recreation Center, Building 43"
        entry["timing"]["start_local"] = f"2026-09-{16 + index}T10:00:00"
        entry["timing"]["end_local"] = f"2026-09-{16 + index}T{15 if index < 2 else 14}:00:00"
        expected.append(entry)

    request["assembly_scope"]["selected_entry_ids"] = [
        entry["import_entry_id"] for entry in expected
    ]

    stage = StageChangesInput.model_validate(request)
    # No hand-authored Item/Time/Event/route support variants are needed.
    assert stage.assembly_scope is not None
    assert stage.assembly_scope.allowed_mutation_types == []
    assert stage.assembly_scope.planned_create_types == []
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="career-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    staged = service.stage(stage, assembly_operation_token=token, assembly_argument_hash="a" * 64)
    assert staged["assembly_ready"] is True, staged
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="career-commit",
        ordinal=2,
        tool_name="docket_commit_changeset",
        argument_hash="b" * 64,
    )
    committed = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=token,
        assembly_argument_hash="b" * 64,
    )
    assert committed["disposition"] == "committed", committed
    assert committed["provider_operation_count"] == 3
    assert committed["delivery_status"] == {
        "tool": "docket_get_history_entry",
        "arguments": {"ref": committed["changeset_ref"], "view": "delivery"},
        "when": "provider_status_needed",
    }
    events = sorted(
        session.scalars(select(CanonicalEvent)),
        key=lambda row: row.event_spec["timing"]["start_local"],
    )
    assert len(events) == 3
    for event, entry in zip(events, expected, strict=True):
        assert event.title == event.event_spec["title"] == entry["title"]
        assert {
            key: value for key, value in event.event_spec["timing"].items() if value is not None
        } == entry["timing"]
        assert event.event_spec["location"] == entry["location"]
        assert not event.event_spec.get("recurrence")
        assert event.lane_ref == lane.ref_id
        assert event.event_spec["calendar_lane"] == "meetings"
    items = {item.ref_id: item for item in session.scalars(select(Item))}
    times = sorted(
        session.scalars(select(TemporalBinding)), key=lambda row: row.temporal_value["start_local"]
    )
    assert len(items) == len(times) == 3
    for temporal, entry in zip(times, expected, strict=True):
        assert items[temporal.subject_ref].title == entry["title"]
        assert temporal.temporal_value["start_local"] == entry["timing"]["start_local"]
        assert temporal.temporal_value["end_local"] == entry["timing"]["end_local"]
        assert temporal.temporal_value["timezone"] == "America/Los_Angeles"
    operations = list(session.scalars(select(Operation)))
    assert len(operations) == 3
    assert all(operation.status == "pending" for operation in operations)
    assert {ref for operation in operations for ref in operation.canonical_target_refs} == {
        event.ref_id for event in events
    }
    assert session.scalar(select(func.count(AssemblyOperation.id))) == 2


@pytest.mark.integration
def test_failed_entry_preserves_entire_draft_and_repairs_without_new_request(
    session, monkeypatch
) -> None:
    import docket.services.changeset_assembly as assembly_module

    utterance = _utterance("1542799000000000841")
    source, lane = _schedule_context(session, utterance, suffix="retained-entry")
    stage = _schedule_stage(
        utterance,
        source_ref=source.ref_id,
        lane_ref=lane.ref_id,
        start_index=0,
        count=3,
        include_scope=True,
    )
    stage.expected_versions = {lane.ref_id: lane.version}
    compiler = assembly_module.compile_normalized_entry

    def faulty_compiler(entry, **kwargs):
        if entry.import_entry_id == "math-1263-entry-01":
            raise DocketError(
                code="normalized_entry_lane_unresolved",
                message="Injected transient compiler error",
                details={"field_path": ["lane_ref"], "next_action": "resolve_lane"},
            )
        return compiler(entry, **kwargs)

    monkeypatch.setattr(assembly_module, "compile_normalized_entry", faulty_compiler)
    trace_ref = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="save-batch",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    saved = service.stage(stage, assembly_operation_token=token, assembly_argument_hash="a" * 64)
    assert saved["disposition"] == "saved_with_errors", saved
    assert saved["assembly_ready"] is False
    assert saved["normalized_entry_count"] == len(saved["entry_preview"]) == 3
    assert saved["omitted_entry_count"] == 0
    assert saved["diagnostic_sample"][0] == {
        "code": "normalized_entry_lane_unresolved",
        "category": "domain_validation",
        "entry_id": "math-1263-entry-01",
        "field_path": ["lane_ref"],
        "constraint": "normalized_entry_lane_unresolved",
        "next_action": "resolve_lane",
    }
    draft = session.scalar(select(ChangeSet))
    assert draft is not None
    assert len(draft.normalized_entries_json) == 3
    assert draft.staged_actions_json is not None and len(draft.staged_actions_json) == 8
    assert draft.expected_versions == stage.expected_versions
    failed_snapshot = session.scalar(select(ChangeSetRevision))
    assert failed_snapshot.staged_actions_json == draft.staged_actions_json
    assert len(failed_snapshot.normalized_entries_json) == 3
    original_hash = draft.authority_scope_hash
    original_ref = draft.semantic_request_ref
    original_entries = json.dumps(draft.normalized_entries_json, sort_keys=True)
    malformed = stage.model_dump(mode="json")
    malformed["patch"]["operations"][1]["entry"]["title"] = None
    with pytest.raises(ValidationError):
        StageChangesInput.model_validate(malformed)
    assert json.dumps(draft.normalized_entries_json, sort_keys=True) == original_entries
    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="blocked-commit",
        ordinal=2,
        tool_name="docket_commit_changeset",
        argument_hash="b" * 64,
    )
    blocked = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="b" * 64,
    )
    assert blocked["disposition"] == "rejected_validation"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"
    assert session.scalar(select(SemanticRequest)).commit_state == "blocked_validation"

    monkeypatch.setattr(assembly_module, "compile_normalized_entry", compiler)
    repair = stage.model_copy(deep=True)
    repair.assembly_scope = None
    repair.patch.operations = [repair.patch.operations[1]]
    repair_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="repair-entry",
        ordinal=3,
        tool_name="docket_stage_changes",
        argument_hash="c" * 64,
    )
    repaired = service.stage(
        repair, assembly_operation_token=repair_token, assembly_argument_hash="c" * 64
    )
    assert repaired["disposition"] == "ready_to_commit", repaired
    assert repaired["normalized_entry_count"] == 3
    assert draft.authority_scope_hash == original_hash
    assert draft.semantic_request_ref == original_ref
    assert draft.current_revision == 2
    assert failed_snapshot.validation_errors_json
    # Lost-response retry returns the original outcome, without restoring its error.
    replay = service.stage(stage, assembly_operation_token=token, assembly_argument_hash="a" * 64)
    assert replay["disposition"] == "saved_with_errors" and replay["replayed"] is True
    assert draft.current_revision == 2 and draft.state == "validated"
    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="repaired-commit",
        ordinal=4,
        tool_name="docket_commit_changeset",
        argument_hash="d" * 64,
    )
    committed = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="d" * 64,
    )
    assert committed["disposition"] == "committed", committed
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 3
    assert session.scalar(select(func.count(Operation.id))) == 3
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1


@pytest.mark.integration
def test_global_compilation_failure_retains_action_inputs_for_new_operation(
    session, monkeypatch
) -> None:
    utterance = _utterance("1542799000000000842")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    original_compile = service.changesets._compile_required_provider_intents

    def fail_compile(*args, **kwargs):
        raise DocketError(code="injected_provider_compilation_error", message="Synthetic failure")

    monkeypatch.setattr(service.changesets, "_compile_required_provider_intents", fail_compile)
    trace_ref = new_public_ref("trace")
    stage = _item_stage(utterance)
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="global-failure",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    failed = service.stage(stage, assembly_operation_token=token, assembly_argument_hash="a" * 64)
    assert failed["disposition"] == "saved_with_errors"
    assert failed["totals"] == {"item": 1}
    draft = session.scalar(select(ChangeSet))
    assert draft.staged_actions_json[0]["create_spec"]["title"] == "Tracked request"
    assert session.scalar(select(func.count(Item.id))) == 0
    monkeypatch.setattr(service.changesets, "_compile_required_provider_intents", original_compile)
    repair_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="global-repair",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="b" * 64,
    )
    repaired = service.stage(
        stage, assembly_operation_token=repair_token, assembly_argument_hash="b" * 64
    )
    assert repaired["disposition"] == "ready_to_commit"
    assert repaired["current_revision"] == 2
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1


@pytest.mark.integration
@pytest.mark.parametrize("long_title", [False, True])
def test_import_title_error_identifies_fields_and_preserves_entire_draft(
    session, monkeypatch, long_title
) -> None:
    import docket.services.changeset_assembly as assembly_module
    from docket.mcp.instrumented import _compact_result

    utterance = _utterance("1542799000000000901")
    source, lane = _schedule_context(session, utterance, suffix="title-diagnostic")
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    compiler = assembly_module.compile_normalized_entry
    wrong_title = "🙃" * 512 if long_title else "Incorrect generic class"

    def broken_title(entry, **kwargs):
        compiled = compiler(entry, **kwargs)
        actions = deepcopy(compiled.actions)
        for action in actions:
            if action["mutation_type"] == "canonical_event_create":
                action["create_spec"]["event_spec"]["title"] = wrong_title
        return replace(compiled, actions=actions)

    monkeypatch.setattr(assembly_module, "compile_normalized_entry", broken_title)
    initial = _schedule_stage(
        utterance, source_ref=source.ref_id, lane_ref=lane.ref_id,
        start_index=0, count=3, include_scope=True,
    )
    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="title-stage",
        ordinal=1, tool_name="docket_stage_changes", argument_hash="1" * 64,
    )
    saved = service.stage(initial, assembly_operation_token=token, assembly_argument_hash="1" * 64)
    assert saved["disposition"] == "saved_with_errors"
    draft = session.scalar(select(ChangeSet))
    original_request, original_authority = draft.semantic_request_ref, draft.authority_scope_hash
    assert len(draft.normalized_entries_json) == 3
    errors = [
        row for row in draft.validation_errors
        if row["code"] == "import_entry_calendar_title_mismatch"
    ]
    assert len(errors) == 3
    for index, error in enumerate(errors):
        entry = initial.patch.operations[index].entry
        assert error["entry_id"] == entry.import_entry_id
        assert error["change_id"] == f"{entry.import_entry_id}.event"
        assert error["field_path"] == ["create_spec", "event_spec", "title"]
        assert error["constraint"] == "event_title_equals_linked_item_title"
        assert error["category"] == "domain_validation"
        assert error["next_action"] == "repair_staged_entry"
        assert error["details"]["comparison"] == {
            "actual": wrong_title,
            "expected": entry.title,
            "expected_change_id": f"{entry.import_entry_id}.item",
            "expected_field_path": ["create_spec", "title"],
            "basis": "staged_item_not_independent_source_verification",
        }
    assert saved["omitted_diagnostic_count"] == (
        saved["diagnostic_count"] - len(saved["diagnostic_sample"])
    )
    if long_title:
        # Compact baseline diagnostics may still fit when the copied mismatch
        # values do not. Missing large details remain reachable through review.
        assert saved["omitted_diagnostic_count"] > 0
        assert all(row["code"] == "source_interpretation_compilation_mismatch"
                   for row in saved["diagnostic_sample"])
    _, exposed = _compact_result(saved, saved, audit=False, page_limit=25)
    assert exposed["disposition"] == "saved_with_errors"
    assert len(json.dumps(exposed, ensure_ascii=False).encode()) <= 16384

    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="title-commit",
        ordinal=2, tool_name="docket_commit_changeset", argument_hash="2" * 64,
    )
    blocked = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        assembly_operation_token=token, assembly_argument_hash="2" * 64,
    )
    assert blocked["disposition"] == "rejected_validation"
    assert blocked["error"]["details"]["diagnostic_review"] == saved["diagnostic_review"]
    _, exposed = _compact_result(blocked, blocked, audit=False, page_limit=25)
    assert exposed["disposition"] == "rejected_validation"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0

    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="old-title-diagnostics",
        ordinal=3, tool_name="docket_review_changeset", argument_hash="3" * 64,
    )
    review = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            **saved["diagnostic_review"]["arguments"],
        ),
        assembly_operation_token=token, assembly_argument_hash="3" * 64,
    )
    assert review["revision"] == review["current_revision"] == 1
    assert review["diagnostic_count"] == saved["diagnostic_count"]
    assert review["items"] and review["state"] == "draft"
    assert draft.semantic_request_ref == original_request
    assert draft.authority_scope_hash == original_authority
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"
    assert session.scalar(select(func.count(SemanticRequest.id))) == 1


@pytest.mark.integration
def test_oversized_diagnostic_is_saved_and_losslessly_reviewable(session, monkeypatch) -> None:
    from docket.mcp.instrumented import _compact_result

    utterance = _utterance("1542799000000000902")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    diagnostic = {
        "code": "fixture_compound_validation",
        "category": "domain_validation",
        "change_id": "tracked-request",
        "field_path": ["create_spec", "title"],
        "constraint": "fixture_text_comparison",
        "next_action": "repair_staged_actions",
        "details": {"comparison": {"actual": "🙂é" * 6000, "expected": "Fixture"}},
    }
    validator = service.changesets._validate
    monkeypatch.setattr(service.changesets, "_validate", lambda *a, **kw: [diagnostic])
    trace_ref = new_public_ref("trace")
    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="huge-error",
        ordinal=1, tool_name="docket_stage_changes", argument_hash="1" * 64,
    )
    saved = service.stage(
        _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="1" * 64,
    )
    assert saved["disposition"] == "saved_with_errors"
    assert saved["diagnostic_count"] == saved["omitted_diagnostic_count"] == 1
    assert saved["diagnostic_sample"] == []
    draft = session.scalar(select(ChangeSet))
    assert draft.validation_errors == [diagnostic]
    assert session.scalar(select(ChangeSetRevision)).validation_errors_json == [diagnostic]
    cursor = saved["diagnostic_review"]["arguments"]["cursor"]
    fragments = []
    # A new successful validation does not erase the failed receipt's snapshot.
    monkeypatch.setattr(service.changesets, "_validate", validator)
    token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="corrected-validation",
        ordinal=2, tool_name="docket_stage_changes", argument_hash="2" * 64,
    )
    repaired = service.stage(
        _item_stage(utterance), assembly_operation_token=token, assembly_argument_hash="2" * 64,
    )
    assert repaired["disposition"] == "ready_to_commit" and repaired["current_revision"] == 2
    assert repaired["diagnostic_count"] == repaired["omitted_diagnostic_count"] == 0
    assert "diagnostic_review" not in repaired
    ordinal = 2
    while cursor:
        ordinal += 1
        digest = sha256_json({"diagnostic_page": ordinal})
        token = _admit(
            session, utterance=utterance, trace_ref=trace_ref, call_id=f"diagnostic-{ordinal}",
            ordinal=ordinal, tool_name="docket_review_changeset", argument_hash=digest,
        )
        page = service.review(
            ReviewChangesInput(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                view="diagnostics", cursor=cursor,
            ),
            assembly_operation_token=token, assembly_argument_hash=digest,
        )
        assert page["logical_detail_count"] == 1 and page["revision"] == 1
        assert page["current_revision"] == 2 and page["state"] == "draft"
        assert page["items"] and page["count"] == len(page["items"])
        _, exposed = _compact_result(page, page, audit=False, page_limit=25)
        assert exposed["disposition"] == "reviewed"
        assert exposed["count"] == page["count"]  # no transport-side row loss
        assert len(json.dumps(exposed, ensure_ascii=False).encode()) <= 16384
        fragments.extend(row["detail_fragment"] for row in page["items"])
        cursor = page.get("cursor")
    encoded = "".join(fragment["text"] for fragment in fragments).encode()
    assert json.loads(encoded) == diagnostic
    assert all(hashlib.sha256(encoded).hexdigest() == f["sha256"] for f in fragments)
    assert len(fragments) == page["total_if_known"]
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(SemanticRequest)).authority_availability == "available"


def test_compiler_error_preserves_qualified_category_and_constraint():
    rows = ChangeSetAssemblyService._compilation_diagnostics(
        DocketError(
            code="semantic_projection_unresolved", message="Do not copy exception prose",
            details={
                "category": "implementation_validation",
                "entry_id": "entry-1", "change_id": "event-1",
                "field_path": ["create_spec", "item_change_ids"],
                "constraint": "dependency_target_exists", "next_action": "repair_staged_entry",
                "unsafe_exception_payload": "DO NOT COPY",
            },
        )
    )
    assert rows == [{
        "code": "semantic_projection_unresolved", "category": "implementation_validation",
        "entry_id": "entry-1", "change_id": "event-1",
        "field_path": ["create_spec", "item_change_ids"],
        "constraint": "dependency_target_exists", "next_action": "repair_staged_entry",
    }]


@pytest.mark.parametrize(
    ("broken_field", "field_path", "constraint"),
    [
        ("item", ["create_spec", "item_change_ids"], "event_links_entry_item"),
        (
            "time", ["create_spec", "realizes_temporal_binding_change_ids"],
            "event_realizes_entry_time",
        ),
        (
            "recurrence", ["create_spec", "event_spec", "recurrence"],
            "source_entry_is_one_occurrence",
        ),
        ("basis", ["basis_refs"], "calendar_has_entry_statement_basis"),
        ("type", ["mutation_type"], "calendar_mutation_matches_representation"),
    ],
)
def test_calendar_import_constraints_have_distinct_repair_paths(
    broken_field, field_path, constraint
) -> None:
    from docket.models import InterpretedStatement
    from docket.schemas.authority import (
        CanonicalEventCreate,
        ImportScope,
        ItemCreate,
        TemporalBindingCreate,
    )
    from docket.services.change_sets import ChangeSetService
    from docket.services.changeset_compiler import compile_normalized_entry

    utterance = _utterance("1542799000000000903")
    utterance.ref_id = new_public_ref("utt")
    source_ref, lane_ref, statement_ref = (
        new_public_ref("src"), new_public_ref("lane"), new_public_ref("stm")
    )
    entry = _schedule_stage(
        utterance, source_ref=source_ref, lane_ref=lane_ref,
        start_index=0, count=1, include_scope=True,
    ).patch.operations[0].entry
    compiled = compile_normalized_entry(
        entry, utterance_ref=utterance.ref_id, statement_ref=statement_ref,
        calendar_lane="meetings",
    )
    raw = deepcopy(compiled.actions[2])
    if broken_field == "item":
        raw["create_spec"]["item_change_ids"] = []
    elif broken_field == "time":
        raw["create_spec"]["realizes_temporal_binding_change_ids"] = []
    elif broken_field == "recurrence":
        raw["create_spec"]["event_spec"]["recurrence"] = {
            "frequency": "daily", "interval": 1, "count": 2,
        }
    elif broken_field == "basis":
        raw["basis_refs"] = [utterance.ref_id]
    coverage = compiled.coverage.model_copy(update={
        "calendar_change_id": compiled.coverage.item_change_id
    }) if broken_field == "type" else compiled.coverage
    errors = ChangeSetService._import_entry_coverage_errors(
        scope=ImportScope(source_refs=[source_ref], entry_coverage=[coverage]),
        changes=[
            ItemCreate.model_validate(compiled.actions[0]),
            TemporalBindingCreate.model_validate(compiled.actions[1]),
            CanonicalEventCreate.model_validate(raw),
        ],
        statements={statement_ref: InterpretedStatement(
            ref_id=statement_ref, source_ref=source_ref,
            interpretation_json={"import_entry_id": entry.import_entry_id},
            source_fragment_locator=entry.evidence.source_fragment_locator,
            source_fragment_hash=entry.evidence.source_fragment_hash,
        )},
        attachment_source_refs={source_ref},
    )
    diagnostic = next(row for row in errors if row.get("constraint") == constraint)
    assert diagnostic["entry_id"] == entry.import_entry_id
    assert diagnostic["change_id"] == coverage.calendar_change_id
    assert diagnostic["field_path"] == field_path
    assert diagnostic["category"] == "domain_validation"
    assert diagnostic["next_action"] == "repair_staged_entry"
    assert "comparison" not in diagnostic["details"]


@pytest.mark.integration
def test_old_stage_retry_cannot_undo_newer_replacement(session) -> None:
    utterance = _utterance("1542799000000000804")
    session.add(utterance)
    session.flush()
    trace_a = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    original = _item_stage(utterance)
    original_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="old-item-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    first = service.stage(
        original,
        assembly_operation_token=original_token,
        assembly_argument_hash="1" * 64,
    )
    assert first["current_revision"] == 1

    trace_b = new_public_ref("trace")
    review_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_b,
        call_id="replacement-review",
        ordinal=1,
        tool_name="docket_review_changeset",
        argument_hash="2" * 64,
    )
    reviewed = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
        ),
        assembly_operation_token=review_token,
        assembly_argument_hash="2" * 64,
    )
    assert reviewed["revision"] == 1

    replacement_payload = original.model_dump(mode="json", exclude_none=True)
    replacement_payload["patch"]["operations"][0]["action"]["create_spec"]["title"] = (
        "Tracked request V2"
    )
    replacement = StageChangesInput.model_validate(replacement_payload)
    replacement_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_b,
        call_id="replacement-stage",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="3" * 64,
    )
    replaced = service.stage(
        replacement,
        assembly_operation_token=replacement_token,
        assembly_argument_hash="3" * 64,
    )
    assert replaced["current_revision"] == 2
    assert replaced["replaced_count"] == 1

    retry = service.stage(
        original,
        assembly_operation_token=original_token,
        assembly_argument_hash="1" * 64,
    )
    assert retry["replayed"] is True
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None and changeset.current_revision == 2
    action = changeset.tracked_context_changes[0]
    assert action["create_spec"]["title"] == "Tracked request V2"

    cross_trace_admission = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=new_public_ref("trace"),
        upstream_tool_call_id="old-item-stage",
        trace_ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
        guild_id=get_settings().discord_guild_id,
        channel_id=get_settings().chat_channel_id,
        source_message_id=utterance.request_key.split(":")[3],
        actor_id=get_settings().operator_discord_user_id,
    )
    assert cross_trace_admission["replayed"] is True
    assert cross_trace_admission["assembly_operation_token"] == original_token

    with pytest.raises(DocketError) as exc_info:
        ChangeSetAssemblyAdmissionService(session).admit(
            utterance_ref=utterance.ref_id,
            trace_ref=trace_a,
            upstream_tool_call_id="old-item-stage",
            trace_ordinal=1,
            tool_name="docket_stage_changes",
            argument_hash="9" * 64,
            guild_id=get_settings().discord_guild_id,
            channel_id=get_settings().chat_channel_id,
            source_message_id=utterance.request_key.split(":")[3],
            actor_id=get_settings().operator_discord_user_id,
        )
    assert getattr(exc_info.value, "code", None) == "stage_idempotency_mismatch"


@pytest.mark.integration
def test_source_reinterpretation_is_not_repair_and_removal_clears_owned_actions(session) -> None:
    utterance = _utterance("1542799000000000805")
    source, lane = _schedule_context(session, utterance, suffix="replace-entry")
    trace_ref = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    scheduled = _schedule_stage(
        utterance,
        source_ref=source.ref_id,
        lane_ref=lane.ref_id,
        start_index=0,
        count=1,
        include_scope=True,
    )
    first_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="scheduled-entry",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    assert service.stage(
        scheduled,
        assembly_operation_token=first_token,
        assembly_argument_hash="1" * 64,
    )["totals"] == {
        "canonical_event": 1,
        "item": 1,
        "lane_routing_decision": 1,
        "temporal_binding": 1,
    }

    replacement_payload = scheduled.model_dump(mode="json", exclude_none=True)
    replacement_payload.pop("assembly_scope", None)
    entry = replacement_payload["patch"]["operations"][0]["entry"]
    entry["entry_type"] = "schedule_exception_entry"
    for field in ("title", "kind", "timing", "lane_ref", "context_entity_refs"):
        entry.pop(field, None)
    entry["item"] = {"title": "No Class", "kind": "schedule.exception"}
    entry["temporal"] = {
        "role": "scheduled_on",
        "binding_key": "default",
        "temporal_value": {
            "kind": "date",
            "date": "2026-09-08",
            "timezone": "America/Los_Angeles",
        },
    }
    entry["exception_disposition"] = "no_occurrence"
    entry["calendar"] = {"kind": "none"}
    replacement = StageChangesInput.model_validate(replacement_payload)
    second_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="exception-entry",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="2" * 64,
    )
    with pytest.raises(DocketError) as conflict:
        service.stage(replacement, assembly_operation_token=second_token,
                      assembly_argument_hash="2" * 64)
    assert conflict.value.code == "request_interpretation_conflict"
    service.reject_admitted_operation(
        token=second_token, argument_hash="2" * 64, operation_kind="stage",
        utterance_ref=utterance.ref_id, error=conflict.value,
    )
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None
    assert changeset.current_revision == 1
    assert len(changeset.event_changes) == 1

    forbidden_payload = replacement.model_dump(mode="json", exclude_none=True)
    forbidden_payload["patch"] = {
        "operations": [{"operation": "action_remove", "change_id": "math-1263-entry-00.item"}]
    }
    forbidden = StageChangesInput.model_validate(forbidden_payload)
    forbidden_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="direct-owned-edit",
        ordinal=3,
        tool_name="docket_stage_changes",
        argument_hash="3" * 64,
    )
    with pytest.raises(DocketError) as exc_info:
        service.stage(
            forbidden,
            assembly_operation_token=forbidden_token,
            assembly_argument_hash="3" * 64,
        )
    assert getattr(exc_info.value, "code", None) == "compiler_owned_action"
    service.reject_admitted_operation(
        token=forbidden_token, argument_hash="3" * 64, operation_kind="stage",
        utterance_ref=utterance.ref_id, error=exc_info.value,
    )
    removal = replacement.model_dump(mode="json", exclude_none=True)
    removal["patch"] = {"operations": [{
        "operation": "normalized_entry_remove", "import_entry_id": "math-1263-entry-00",
    }]}
    removal_token = _admit(
        session, utterance=utterance, trace_ref=trace_ref, call_id="remove-selected-entry",
        ordinal=4, tool_name="docket_stage_changes", argument_hash="4" * 64,
    )
    removed = service.stage(StageChangesInput.model_validate(removal),
                            assembly_operation_token=removal_token, assembly_argument_hash="4" * 64)
    assert removed["disposition"] == "saved_with_errors"
    assert removed["source_interpretation"]["missing_entry_count"] == 1
    assert changeset.event_changes == changeset.lane_changes == changeset.provider_intents == []
    assert changeset.staged_actions_json == changeset.compiled_action_ownership_json == []
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 2


@pytest.mark.integration
@pytest.mark.parametrize("view", ["actions", "diff"])
def test_review_cursor_stays_on_one_revision_and_does_not_advance_observation(
    session, view
) -> None:
    utterance = _utterance("1542799000000000806")
    session.add(utterance)
    session.flush()
    trace_a = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)
    item_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="cursor-item",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    service.stage(
        _item_stage(utterance),
        assembly_operation_token=item_token,
        assembly_argument_hash="1" * 64,
    )
    task_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="cursor-task",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="2" * 64,
    )
    service.stage(
        _task_stage(utterance),
        assembly_operation_token=task_token,
        assembly_argument_hash="2" * 64,
    )
    page_one_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="cursor-page-one",
        ordinal=3,
        tool_name="docket_review_changeset",
        argument_hash="3" * 64,
    )
    page_one = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
            view=view,
            limit=1,
        ),
        assembly_operation_token=page_one_token,
        assembly_argument_hash="3" * 64,
    )
    assert page_one["revision"] == 2
    assert page_one["truncated"] is True

    trace_b = new_public_ref("trace")
    b_review_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_b,
        call_id="cursor-b-review",
        ordinal=1,
        tool_name="docket_review_changeset",
        argument_hash="4" * 64,
    )
    service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
        ),
        assembly_operation_token=b_review_token,
        assembly_argument_hash="4" * 64,
    )
    replacement_payload = _item_stage(utterance).model_dump(mode="json", exclude_none=True)
    replacement_payload["patch"]["operations"][0]["action"]["create_spec"]["title"] = (
        "Concurrent V3"
    )
    b_stage_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_b,
        call_id="cursor-b-stage",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="5" * 64,
    )
    service.stage(
        StageChangesInput.model_validate(replacement_payload),
        assembly_operation_token=b_stage_token,
        assembly_argument_hash="5" * 64,
    )

    page_two_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="cursor-page-two",
        ordinal=4,
        tool_name="docket_review_changeset",
        argument_hash="6" * 64,
    )
    page_two = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
            view=view,
            limit=1,
            cursor=page_one["cursor"],
        ),
        assembly_operation_token=page_two_token,
        assembly_argument_hash="6" * 64,
    )
    assert page_two["revision"] == 2
    assert page_two["current_revision"] == 3
    assert page_two["is_current_revision"] is False
    assert page_two["totals"] == {"item": 1, "task": 1}
    assert page_one["items"] != page_two["items"]
    if view == "diff":
        assert page_one["diff_basis"] == page_two["diff_basis"] == "previous_draft_revision"
        assert page_one["base_revision"] == page_two["base_revision"] == 1
        assert page_one["diff_subject_counts"] == {"added": 1, "removed": 0, "modified": 0}
        assert page_one["total_if_known"] == page_two["total_if_known"]
        assert "Concurrent V3" not in json.dumps([page_one, page_two])

    commit_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_a,
        call_id="cursor-stale-commit",
        ordinal=5,
        tool_name="docket_commit_changeset",
        argument_hash="7" * 64,
    )
    conflict = service.commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=commit_token,
        assembly_argument_hash="7" * 64,
    )
    assert conflict["disposition"] == "draft_revision_conflict"
    assert conflict["error"]["details"]["observed_revision"] == 2


@pytest.mark.integration
def test_diff_exposes_entry_values_removal_and_lossless_large_field(session) -> None:
    utterance = _utterance("1542799000000000898")
    source, lane = _schedule_context(session, utterance, suffix="diff-values")
    service = ChangeSetAssemblyService(session)
    trace_ref = new_public_ref("trace")
    ordinal = 0

    def stage(request):
        nonlocal ordinal
        ordinal += 1
        digest = sha256_json({"stage": ordinal})
        token = _admit(
            session, utterance=utterance, trace_ref=trace_ref,
            call_id=f"diff-stage-{ordinal}", ordinal=ordinal,
            tool_name="docket_stage_changes", argument_hash=digest,
        )
        return service.stage(request, assembly_operation_token=token, assembly_argument_hash=digest)

    initial = _schedule_stage(
        utterance, source_ref=source.ref_id, lane_ref=lane.ref_id,
        start_index=0, count=3, include_scope=True,
    )
    rich = initial.model_dump(mode="json", exclude_none=True)
    operations = rich["patch"]["operations"]
    entry = operations[0]["entry"]
    entry["title"] = "Exact lecture topic"
    entry["description"] = "🙂é" * 2_000
    entry["location"] = "Building 43"
    assert stage(StageChangesInput.model_validate(rich))["disposition"] == "ready_to_commit"
    replacement = {**rich, "assembly_scope": None}
    replacement["patch"] = {"operations": [{
        "operation": "normalized_entry_remove",
        "import_entry_id": entry["import_entry_id"],
    }]}
    assert stage(StageChangesInput.model_validate(replacement))["disposition"] == (
        "saved_with_errors"
    )
    rows = []
    cursor = None
    while True:
        ordinal += 1
        digest = sha256_json({"review": ordinal})
        token = _admit(
            session, utterance=utterance, trace_ref=trace_ref,
            call_id=f"diff-page-{ordinal}", ordinal=ordinal,
            tool_name="docket_review_changeset", argument_hash=digest,
        )
        page = service.review(
            ReviewChangesInput(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                view="diff", limit=2, cursor=cursor,
            ),
            assembly_operation_token=token, assembly_argument_hash=digest,
        )
        assert page["revision"] == 2 and page["base_revision"] == 1
        assert page["diff_subject_counts"] == {"added": 0, "removed": 1, "modified": 0}
        assert 0 < page["count"] <= 2
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 16 * 1024
        rows.extend(page["items"])
        assert page["omitted_detail_count"] == page["total_if_known"] - len(rows)
        cursor = page.get("cursor")
        if cursor is None:
            break
    assert len(rows) == page["total_if_known"]
    fields = {
        tuple(row["field_path"]): row for row in rows
        if "field_path" in row and row["change"] == "removed"
    }
    assert fields[("title",)]["before"] == "Exact lecture topic"
    assert fields[("title",)]["after_present"] is False
    assert fields[("location",)]["before"] == "Building 43"
    fragments = [row["detail_fragment"] for row in rows if "detail_fragment" in row]
    fragments.sort(key=lambda fragment: fragment["byte_offset"])
    reconstructed = json.loads("".join(fragment["text"] for fragment in fragments))
    assert reconstructed["field_path"] == ["description"]
    assert reconstructed["before"] == "🙂é" * 2_000
    assert any(row.get("change") == "removed" for row in rows)
    assert all(row.get("subject_kind", "entry") == "entry" for row in rows)
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0


@pytest.mark.integration
def test_new_trace_reviews_then_resumes_existing_server_held_draft(session) -> None:
    utterance = _utterance("1542799000000000807")
    session.add(utterance)
    session.flush()
    service = ChangeSetAssemblyService(session)
    first_trace = new_public_ref("trace")
    first_token = _admit(
        session,
        utterance=utterance,
        trace_ref=first_trace,
        call_id="restart-first-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    service.stage(
        _item_stage(utterance),
        assembly_operation_token=first_token,
        assembly_argument_hash="1" * 64,
    )

    resumed_trace = new_public_ref("trace")
    review_token = _admit(
        session,
        utterance=utterance,
        trace_ref=resumed_trace,
        call_id="restart-review",
        ordinal=1,
        tool_name="docket_review_changeset",
        argument_hash="2" * 64,
    )
    reviewed = service.review(
        ReviewChangesInput(
            utterance_ref=utterance.ref_id,
            request_key=utterance.request_key,
        ),
        assembly_operation_token=review_token,
        assembly_argument_hash="2" * 64,
    )
    assert reviewed["revision"] == 1

    task_token = _admit(
        session,
        utterance=utterance,
        trace_ref=resumed_trace,
        call_id="restart-task-stage",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="3" * 64,
    )
    staged = service.stage(
        _task_stage(utterance),
        assembly_operation_token=task_token,
        assembly_argument_hash="3" * 64,
    )
    assert staged["current_revision"] == 2
    assert staged["totals"] == {"item": 1, "task": 1}
    assert session.scalar(select(func.count(ChangeSet.id))) == 1


@pytest.mark.integration
def test_direct_commit_cannot_collide_with_existing_assembled_draft(session) -> None:
    utterance = _utterance("1542799000000000808")
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="collision-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="1" * 64,
    )
    stage_request = _item_stage(utterance)
    ChangeSetAssemblyService(session).stage(
        stage_request,
        assembly_operation_token=token,
        assembly_argument_hash="1" * 64,
    )
    action = stage_request.patch.operations[0].action
    result = InteractiveAuthorityService(session).process_turn(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        actor_id=get_settings().operator_discord_user_id,
        intent_session_ref=None,
        expected_session_version=None,
        statements=[],
        relations=[],
        resolved_intent_json={"intent": "direct collision"},
        blocking_clarifications=[],
        content=ChangeSetContent(
            basis_refs=[utterance.ref_id],
            tracked_context_changes=[action],
        ),
        changeset_ref=None,
        expected_changeset_version=None,
    )
    assert result["disposition"] == "assembled_draft_exists"
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None and changeset.current_revision == 1
    assert session.scalar(select(func.count(Item.id))) == 0


@pytest.mark.integration
def test_mcp_stage_domain_rejection_is_a_durable_operation_outcome(
    session_factory,
) -> None:
    utterance = _utterance("1542799000000000809")
    with session_factory.begin() as session:
        session.add(utterance)
        session.flush()
        token = _admit(
            session,
            utterance=utterance,
            trace_ref=new_public_ref("trace"),
            call_id="durable-rejection",
            ordinal=1,
            tool_name="docket_stage_changes",
            argument_hash="a" * 64,
        )
    item_operation = _item_stage(utterance).patch.operations[0]
    result = docket_stage_changes(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        patch={"operations": [item_operation]},
        assembly_scope=AssemblyAuthorityScopeInput(
            resolved_intent={"intent": "only tasks are authorized"},
            allowed_mutation_types=["task_create"],
            planned_create_types=["task"],
        ),
        assembly_operation_token=token,
        assembly_argument_hash="a" * 64,
    )
    assert result["disposition"] == "rejected_validation"
    assert result["error"]["code"] == "assembly_scope_violation"
    with session_factory() as session:
        operation = session.scalar(select(AssemblyOperation))
        assert operation is not None
        assert operation.state == "rejected"
        assert operation.result_json == result
        assert session.scalar(select(func.count(ChangeSet.id))) == 0


@pytest.mark.integration
def test_mcp_schema_rejection_terminalizes_admission_and_next_stage_proceeds(
    session_factory,
) -> None:
    utterance = _utterance("1542799000000000811")
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        session.add(utterance)
        session.flush()
        invalid_arguments = _item_stage(utterance).model_dump(mode="json", exclude_none=True)
        invalid_arguments["patch"]["operations"][0]["action"]["create_spec"]["title"] = ""
        invalid_hash = sha256_json(
            {
                key: value
                for key, value in invalid_arguments.items()
                if key not in {"utterance_ref", "request_key"}
            }
        )
        invalid_token = _admit(
            session,
            utterance=utterance,
            trace_ref=trace_ref,
            call_id="invalid-before-service",
            ordinal=1,
            tool_name="docket_stage_changes",
            argument_hash=invalid_hash,
        )

    invalid_result = asyncio.run(
        mcp.call_tool(
            "docket_stage_changes",
            {
                **invalid_arguments,
                "assembly_operation_token": invalid_token,
                "assembly_argument_hash": invalid_hash,
            },
        )
    )
    assert isinstance(invalid_result, tuple)
    assert invalid_result[1]["error"]["code"] == "validation_error"
    with session_factory() as session:
        rejected = session.scalar(
            select(AssemblyOperation).where(
                AssemblyOperation.upstream_tool_call_id == "invalid-before-service"
            )
        )
        assert rejected is not None
        assert rejected.state == "rejected"
        assert rejected.result_disposition == "rejected_validation"

    valid_arguments = _item_stage(utterance).model_dump(mode="json", exclude_none=True)
    valid_hash = sha256_json(
        {
            key: value
            for key, value in valid_arguments.items()
            if key not in {"utterance_ref", "request_key"}
        }
    )
    with session_factory.begin() as session:
        valid_token = _admit(
            session,
            utterance=utterance,
            trace_ref=trace_ref,
            call_id="valid-after-rejection",
            ordinal=2,
            tool_name="docket_stage_changes",
            argument_hash=valid_hash,
        )
    valid_result = asyncio.run(
        mcp.call_tool(
            "docket_stage_changes",
            {
                **valid_arguments,
                "assembly_operation_token": valid_token,
                "assembly_argument_hash": valid_hash,
            },
        )
    )
    assert isinstance(valid_result, tuple)
    assert valid_result[1]["disposition"] == "ready_to_commit"


@pytest.mark.integration
def test_mcp_stage_then_payload_free_commit_and_replay_without_review(session_factory) -> None:
    from docket.tool_contracts import CONTRACT_VERSION, contract_hash

    utterance = _utterance("1542799000000000840")
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        session.add(utterance)
        session.flush()
        stage_arguments = _item_stage(utterance).model_dump(mode="json", exclude_none=True)

    def invoke(name: str, arguments: dict, ordinal: int) -> dict:
        model_hash = sha256_json(
            {
                key: value
                for key, value in arguments.items()
                if key not in {"utterance_ref", "request_key"}
            }
        )
        with session_factory.begin() as session:
            token = _admit(
                session,
                utterance=utterance,
                trace_ref=trace_ref,
                call_id=f"mcp-{ordinal}",
                ordinal=ordinal,
                tool_name=name,
                argument_hash=model_hash,
            )
        now = int(datetime.now(UTC).timestamp())
        encoded = base64.urlsafe_b64encode(json.dumps({
            "format": 1, "trace_ref": trace_ref, "call_id": f"mcp-{ordinal}",
            "ordinal": ordinal, "utterance_ref": utterance.ref_id, "gateway_instance_ref": None,
            "tool_name": name, "argument_hash": model_hash,
            "contract_version": CONTRACT_VERSION, "contract_hash": contract_hash("interactive"),
            "issued_at": now, "expires_at": now + 900,
        }, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(
            get_settings().hermes_to_docket_token().encode(),
            b"docket-mcp-invocation-v1:" + encoded.encode(), hashlib.sha256,
        ).hexdigest()
        result = asyncio.run(
            mcp.call_tool(
                name,
                {
                    **arguments,
                    "assembly_operation_token": token,
                    "assembly_argument_hash": model_hash,
                    "invocation_binding": f"{encoded}.{signature}",
                },
            )
        )
        assert isinstance(result, tuple)
        return result[1]

    assert invoke("docket_stage_changes", stage_arguments, 1)["disposition"] == "ready_to_commit"
    assert invoke("docket_stage_changes", stage_arguments, 1)["replayed"] is True
    binding = {"utterance_ref": utterance.ref_id, "request_key": utterance.request_key}
    # A resumed pre-cutover recipe must be rejected, not ignored or decoded.
    rejected = invoke(
        "docket_commit_changeset",
        {
            **binding,
            "submission": {"commit_mode": "direct", "content": {}},
        },
        2,
    )
    assert rejected["error"]["code"] == "validation_error"
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 0
        obsolete = session.scalar(
            select(AssemblyOperation).where(AssemblyOperation.upstream_tool_call_id == "mcp-2")
        )
        assert obsolete.state == "rejected"
    receipt = invoke("docket_commit_changeset", binding, 3)
    assert receipt["disposition"] == "committed"
    transport_replay = invoke("docket_commit_changeset", binding, 3)
    assert transport_replay["changeset_ref"] == receipt["changeset_ref"]
    replay = invoke("docket_commit_changeset", binding, 4)
    assert replay == receipt
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(Item.title)) == "Tracked request"
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        calls = list(session.scalars(select(ToolInvocation)))
        assert len(calls) == 6
        assert all(call.trace_ref == trace_ref for call in calls)
        assert sum(call.trace_call_id is None for call in calls) == 2
        assert all(call.transport_state == "completed" for call in calls)
        assert all(call.normalized_argument_hash == sha256_json({}) for call in calls[-2:])


@pytest.mark.integration
def test_mcp_clarification_persists_choices_without_canonical_effects(session_factory) -> None:
    utterance = _utterance("1542799000000000841")
    with session_factory.begin() as session:
        session.add(utterance)
        session.flush()
        action = _item_stage(utterance).patch.operations[0].action.model_dump(mode="json")
    result = asyncio.run(
        mcp.call_tool(
            "docket_request_clarification",
            {
                "utterance_ref": utterance.ref_id,
                "request_key": utterance.request_key,
                "question": "Track this item?",
                "semantic_options": [
                    {
                        "option_id": "track-item",
                        "selection_authority_ref": utterance.ref_id,
                        "content": {
                            "basis_refs": [utterance.ref_id],
                            "tracked_context_changes": [action],
                        },
                    }
                ],
            },
        )
    )
    assert isinstance(result, tuple)
    assert result[1]["disposition"] == "needs_clarification"
    with session_factory() as session:
        assert session.scalar(select(func.count(ChangeSet.id))) == 0
        assert session.scalar(select(func.count(Item.id))) == 0
        call = session.scalar(select(ToolInvocation))
        assert call.domain_state == "succeeded"
        assert call.result_disposition == "needs_clarification"


@pytest.mark.integration
def test_terminal_tool_call_reconciles_stale_admitted_predecessor(session) -> None:
    utterance = _utterance("1542799000000000812")
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stale-predecessor",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    session.add(
        ToolInvocation(
            tool_name="docket_stage_changes",
            tool_contract_version="test",
            tool_contract_hash="0" * 64,
            caller_profile="interactive",
            utterance_refs=[utterance.ref_id],
            received_argument_hash="a" * 64,
            result_disposition="rejected_validation",
            transport_state="completed",
            domain_state="rejected",
            error_code="validation_error",
            trace_ref=trace_ref,
            trace_call_id="stale-predecessor",
            trace_ordinal=1,
            completed_at=datetime.now(UTC),
        )
    )
    valid_token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="stage-after-reconciliation",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash="b" * 64,
    )
    staged = ChangeSetAssemblyService(session).stage(
        _item_stage(utterance),
        assembly_operation_token=valid_token,
        assembly_argument_hash="b" * 64,
    )
    assert staged["disposition"] == "ready_to_commit"
    predecessor = session.scalar(
        select(AssemblyOperation).where(
            AssemblyOperation.upstream_tool_call_id == "stale-predecessor"
        )
    )
    assert predecessor is not None
    assert predecessor.state == "rejected"
    assert predecessor.result_json["reconciled"] is True


@pytest.mark.integration
@pytest.mark.parametrize("later_committed", [False, True])
def test_unknown_stage_operation_reconciles_durable_revision_without_reapplication(
    session, later_committed,
) -> None:
    utterance = _utterance("1542799000000000810")
    session.add(utterance)
    session.flush()
    trace_ref = new_public_ref("trace")
    token = _admit(
        session,
        utterance=utterance,
        trace_ref=trace_ref,
        call_id="unknown-stage",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash="a" * 64,
    )
    service = ChangeSetAssemblyService(session)
    first = service.stage(
        _item_stage(utterance),
        assembly_operation_token=token,
        assembly_argument_hash="a" * 64,
    )
    assert first["current_revision"] == 1
    operation = session.scalar(select(AssemblyOperation))
    assert operation is not None
    if later_committed:
        commit_token = _admit(
            session, utterance=utterance, trace_ref=trace_ref, call_id="later-commit",
            ordinal=2, tool_name="docket_commit_changeset", argument_hash="b" * 64,
        )
        service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            assembly_operation_token=commit_token, assembly_argument_hash="b" * 64,
        )
    operation.state = "unknown"
    operation.result_json = {}
    operation.result_disposition = None
    operation.completed_at = None
    session.flush()

    reconciled = service.stage(
        _item_stage(utterance),
        assembly_operation_token=token,
        assembly_argument_hash="a" * 64,
    )
    assert reconciled["disposition"] == "ready_to_commit"
    assert reconciled["reconciled"] is True
    assert reconciled["current_revision"] == 1
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None and changeset.current_revision == 1
    assert session.scalar(select(func.count(ChangeSetRevision.id))) == 1
    assert session.scalar(select(func.count(Item.id))) == int(later_committed)

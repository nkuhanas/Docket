from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
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
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    Item,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    SemanticRequest,
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
            "resolved_intent": {"intent": "replace course schedule", "entry_count": 30},
            "normalized_entry_types": ["scheduled_occurrence_entry", "schedule_exception_entry"],
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
            ),
            assembly_operation_token=token,
            assembly_argument_hash=argument_hash,
        )
        assert result["disposition"] == "ready_to_commit"
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
def test_normalized_entry_replacement_removes_all_obsolete_owned_actions(session) -> None:
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
    result = service.stage(
        replacement,
        assembly_operation_token=second_token,
        assembly_argument_hash="2" * 64,
    )
    assert result["current_revision"] == 2
    assert result["totals"] == {"item": 1, "temporal_binding": 1}
    assert result["predicted_provider_operation_count"] == 0
    changeset = session.scalar(select(ChangeSet))
    assert changeset is not None
    assert changeset.event_changes == []
    assert changeset.lane_changes == []
    assert changeset.provider_intents == []
    assert changeset.compiled_action_ownership_json[0]["change_ids"] == [
        "math-1263-entry-00.item",
        "math-1263-entry-00.time",
    ]
    revisions = list(
        session.scalars(select(ChangeSetRevision).order_by(ChangeSetRevision.revision))
    )
    assert len(revisions) == 2
    assert len(revisions[0].event_changes) == 1
    assert len(revisions[0].lane_changes) == 1
    assert revisions[1].event_changes == []
    assert revisions[1].lane_changes == []

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
    assert stage(initial)["disposition"] == "ready_to_commit"
    replacement = initial.model_dump(mode="json", exclude_none=True)
    replacement.pop("assembly_scope")
    operations = replacement["patch"]["operations"]
    entry = operations[0]["entry"]
    entry["title"] = "Exact lecture topic"
    entry["description"] = "🙂é" * 2_000
    entry["location"] = "Building 43"
    operations[1] = {
        "operation": "normalized_entry_remove",
        "import_entry_id": operations[1]["entry"]["import_entry_id"],
    }
    replacement["patch"]["operations"] = operations[:2]
    assert stage(StageChangesInput.model_validate(replacement))["disposition"] == "ready_to_commit"
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
        assert page["diff_subject_counts"] == {"added": 0, "removed": 1, "modified": 1}
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
        if "field_path" in row and row["change"] == "modified"
    }
    assert fields[("title",)]["before"] == "MATH 1263 — Topic 1"
    assert fields[("title",)]["after"] == "Exact lecture topic"
    assert fields[("location",)]["after"] == "Building 43"
    fragments = [row["detail_fragment"] for row in rows if "detail_fragment" in row]
    fragments.sort(key=lambda fragment: fragment["byte_offset"])
    reconstructed = json.loads("".join(fragment["text"] for fragment in fragments))
    assert reconstructed["field_path"] == ["description"]
    assert reconstructed["after"] == "🙂é" * 2_000
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
        result = asyncio.run(
            mcp.call_tool(
                name,
                {
                    **arguments,
                    "assembly_operation_token": token,
                    "assembly_argument_hash": model_hash,
                },
            )
        )
        assert isinstance(result, tuple)
        return result[1]

    assert invoke("docket_stage_changes", stage_arguments, 1)["disposition"] == "ready_to_commit"
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
    replay = invoke("docket_commit_changeset", binding, 4)
    assert replay == receipt
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(Item.title)) == "Tracked request"
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        calls = list(session.scalars(select(ToolInvocation)))
        assert len(calls) == 4
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
def test_unknown_stage_operation_reconciles_durable_revision_without_reapplication(
    session,
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

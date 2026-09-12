import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    EventOccurrence,
    LaneRoutingDecision,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    ProviderEventBinding,
    SemanticRequest,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.authority import (
    CanonicalEventCancel,
    CanonicalEventCreate,
    CanonicalEventModify,
    ChangeSetContent,
)
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.canonical_events import CanonicalEventAuthorityService
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.changeset_previews import capture_event_preview, event_preview_sample
from docket.services.event_occurrences import identity_for_timing, occurrence_timing


def _utterance(session, text, number, *, said_at=None):
    settings = get_settings()
    message_id = str(1542799000000000900 + number)
    utterance = OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}",
        conversation_ref=f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}",
        said_at=said_at or datetime.now(UTC),
        verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0",
    )
    session.add(utterance)
    session.flush()
    return utterance


def _world(session):
    utterance = _utterance(session, "Create my recurring MATH class.", 1)
    account = ProviderAccount(
        provider="google",
        external_account_id="occurrence-test",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    lane = CalendarLane(
        account_id=account.id,
        lane="math-1263",
        display_name="MATH 1263",
        calendar_id="math@example.com",
        status="active",
        color_hex="#3367D6",
        basis_refs=[utterance.ref_id],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(lane)
    session.flush()
    spec = StandaloneCalendarEventInput.model_validate(
        {
            "title": "MATH 1263",
            "calendar_lane": "math-1263",
            "timing": {
                "kind": "timed",
                "start_local": "2026-08-24T15:00:00",
                "end_local": "2026-08-24T15:50:00",
                "timezone": "America/Los_Angeles",
            },
            "recurrence": {
                "frequency": "weekly",
                "weekdays": ["MO", "TU", "TH", "FR"],
                "until_date": "2026-10-14",
                "excluded_dates": ["2026-09-07"],
            },
        }
    )
    series = CanonicalEvent(
        canonical_key="recurring-class",
        title=spec.title,
        event_spec=spec.model_dump(mode="json"),
        status="active",
        authority="explicit_operator",
        lane_ref=lane.ref_id,
        lane_id=lane.id,
        basis_refs=[utterance.ref_id],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(series)
    session.flush()
    route = LaneRoutingDecision(
        event_ref=series.ref_id,
        lane_ref=lane.ref_id,
        lane_id=lane.id,
        decision_kind="explicit_operator",
        operator_confirmed=True,
        basis_refs=[utterance.ref_id],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    binding = ProviderEventBinding(
        canonical_target_ref=series.ref_id,
        target_kind="event",
        account_id=account.id,
        calendar_id=lane.calendar_id,
        provider_event_id="recurring-master",
        status="active",
    )
    session.add_all([route, binding])
    session.flush()
    series.routing_decision_ref = route.ref_id
    return series, identity_for_timing(series.ref_id, occurrence_timing(spec, date(2026, 9, 8)))


def _admit(session, utterance, trace, kind, ordinal):
    from trace_support import bind_execution

    settings = get_settings()
    binding = bind_execution(session, utterance, label=trace)
    return ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=binding["trace_ref"],
        **{key: binding[key] for key in (
            "execution_index", "execution_completion_token", "gateway_instance_ref",
        )},
        upstream_tool_call_id=f"{kind}-{ordinal}",
        trace_ordinal=ordinal,
        tool_name=f"docket_{kind}",
        argument_hash=str(ordinal) * 64,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        source_message_id=utterance.request_key.split(":")[3],
        actor_id=settings.operator_discord_user_id,
    )["assembly_operation_token"]


def _stage_cancel(session, series, scope, number=2, event_spec=None):
    utterance = _utterance(session, "This class is cancelled tomorrow.", number)
    trace = new_public_ref("trace")
    token = _admit(session, utterance, trace, "stage_changes", 1)
    result = ChangeSetAssemblyService(session).stage(
        StageChangesInput.model_validate(
            {
                "utterance_ref": utterance.ref_id,
                "request_key": utterance.request_key,
                "expected_versions": {series.ref_id: series.version},
                "assembly_scope": {
                    "resolved_intent": {"intent": "cancel selected occurrence"},
                    "allowed_mutation_types": [
                        "canonical_event_modify" if event_spec else "canonical_event_cancel"
                    ],
                    "target_refs": [series.ref_id],
                    "event_scopes": {series.ref_id: scope},
                },
                "patch": {
                    "operations": [
                        {
                            "operation": "action_upsert",
                            "action": {
                                "mutation_type": "canonical_event_modify"
                                if event_spec
                                else "canonical_event_cancel",
                                "change_id": "cancel-class",
                                "action": "update" if event_spec else "retract",
                                "object_type": "canonical_event",
                                "object_ref": series.ref_id,
                                "payload": {"event_spec": event_spec} if event_spec else {},
                                "scope": scope,
                                "affected_fields": ["status"],
                                "basis_refs": [utterance.ref_id],
                            },
                        }
                    ]
                },
            }
        ),
        assembly_operation_token=token,
        assembly_argument_hash="1" * 64,
    )
    return utterance, trace, result


def _commit(session, utterance, trace):
    token = _admit(session, utterance, trace, "commit_changeset", 2)
    return ChangeSetAssemblyService(session).commit(
        utterance_ref=utterance.ref_id,
        request_key=utterance.request_key,
        assembly_operation_token=token,
        assembly_argument_hash="2" * 64,
    )


def test_occurrence_preview_is_captured_and_paging_does_not_read_live_calendar(session):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, staged = _stage_cancel(session, series, scope)
    preview = staged["event_preview"][0]
    assert staged["event_effect_count"] == 1
    assert preview["scope"] == scope
    assert preview["title"] == "MATH 1263"
    assert preview["status"] == "cancelled"
    assert preview["timing"]["start_local"] == "2026-09-08T15:00:00"
    assert series.status == "active"
    draft = session.scalar(select(ChangeSet))
    snapshot = deepcopy(draft.compiler_manifest_json["canonical_event_preview"])
    effect = snapshot["effects"][0]
    assert effect["before"]["status"] == "active"
    assert effect["after"]["status"] == "cancelled"
    assert effect["before"]["recurrence"] is None
    assert effect["expected_version"] == effect["observed_version"] == 1
    assert "basis_refs" not in json.dumps(snapshot)
    # Later canonical work cannot rewrite an immutable staged preview.
    series.title = "Changed by another committed request"
    series.version += 1
    session.flush()
    reviewed = ChangeSetAssemblyService(session).review(
        ReviewChangesInput(utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                           view="diff", limit=100),
        assembly_operation_token=_admit(session, utterance, trace, "review_changeset", 2),
        assembly_argument_hash="2" * 64,
    )
    assert reviewed["canonical_event_diff_basis"] == "canonical_staging_snapshot"
    rows = [row for row in reviewed["items"]
            if row.get("subject_kind") == "canonical_event_effect"]
    status = next(row for row in rows if row.get("field_path") == ["status"])
    assert status["before"] == "active" and status["after"] == "cancelled"
    assert status["scope"] == scope
    assert "Changed by another" not in json.dumps(reviewed)
    assert draft.compiler_manifest_json["canonical_event_preview"] == snapshot


def test_moved_and_already_cancelled_occurrence_previews_keep_original_identity(session):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    replacement = {
        **series.event_spec, "title": "Rescheduled MATH lecture", "recurrence": None,
        "timing": {
            "kind": "timed", "start_local": "2026-09-09T16:00:00",
            "end_local": "2026-09-09T16:50:00", "timezone": "America/Los_Angeles",
        },
    }
    utterance, trace, staged = _stage_cancel(session, series, scope, event_spec=replacement)
    assert staged["disposition"] == "ready_to_commit", staged
    effect = session.scalar(select(ChangeSet)).compiler_manifest_json["canonical_event_preview"][
        "effects"
    ][0]
    assert effect["before"]["timing"]["start_local"] == "2026-09-08T15:00:00"
    assert effect["after"]["timing"]["start_local"] == "2026-09-09T16:00:00"
    assert _commit(session, utterance, trace)["disposition"] == "committed"
    occurrence = session.scalar(select(EventOccurrence))
    master_binding = session.scalar(select(ProviderEventBinding))
    # Model the completed child projection in this isolated fixture. A child
    # still queued for creation has a separate provider-readiness constraint.
    session.add(ProviderEventBinding(
        canonical_target_ref=occurrence.replacement_event_ref, target_kind="event",
        account_id=master_binding.account_id, calendar_id=master_binding.calendar_id,
        provider_event_id="preview-replacement", status="active",
    ))
    session.flush()
    utterance2, trace2, staged2 = _stage_cancel(session, series, scope, number=3)
    assert staged2["disposition"] == "ready_to_commit", staged2["diagnostic_sample"]
    assert staged2["event_preview"][0]["title"] == "Rescheduled MATH lecture"
    assert staged2["event_preview"][0]["scope"]["identity"]["original_date"] == "2026-09-08"
    assert staged2["event_preview"][0]["timing"]["start_local"] == "2026-09-09T16:00:00"
    assert staged2["event_preview"][0]["status"] == "cancelled"
    receipt2 = _commit(session, utterance2, trace2)
    assert receipt2["disposition"] == "committed", receipt2
    _utterance3, _trace3, staged3 = _stage_cancel(session, series, scope, number=4)
    assert staged3["event_preview"][0]["no_op"] is True
    assert staged3["event_effect_count"] == 1
    assert series.status == "active"


def test_uncompiled_occurrence_never_previews_master_cancellation(session):
    series, identity = _world(session)
    content = ChangeSetContent(
        basis_refs=series.basis_refs,
        event_changes=[CanonicalEventCancel(
            change_id="cancel-only-one", action="retract", object_type="canonical_event",
            object_ref=series.ref_id, basis_refs=series.basis_refs, affected_fields=["status"],
            scope={"kind": "occurrence", "identity": identity.model_dump(mode="json")},
        )],
    )
    effect = capture_event_preview(session, content)["effects"][0]
    assert effect["available"] is False
    assert effect["reason"] == "occurrence_compilation_required"
    assert "after" not in effect
    assert series.status == "active"


@pytest.mark.parametrize(("state", "next_action"), [
    ("pending", "follow_original_provider_delivery"),
    ("running", "follow_original_provider_delivery"),
    ("reconciliation_required", "await_original_provider_reconciliation"),
    ("failed", "recover_original_provider_operation"),
    ("succeeded", "inspect_confirmed_operation_missing_binding"),
])
def test_pending_occurrence_projection_has_exact_recovery_not_new_authority(
    session, state, next_action,
):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    replacement = {
        **series.event_spec, "recurrence": None,
        "timing": {
            "kind": "timed", "start_local": "2026-09-09T16:00:00",
            "end_local": "2026-09-09T16:50:00", "timezone": "America/Los_Angeles",
        },
    }
    utterance, trace, _staged = _stage_cancel(session, series, scope, event_spec=replacement)
    move_receipt = _commit(session, utterance, trace)
    assert move_receipt["disposition"] == "committed"
    create = session.scalar(select(Operation).where(
        Operation.operation_type == "calendar_create_event",
    ))
    create.status = state
    create.last_error_code = "google_auth_invalid" if state == "failed" else None
    session.flush()
    before_operations = session.scalar(select(func.count(Operation.id)))
    before_events = session.scalar(select(func.count(CanonicalEvent.id)))
    utterance2, trace2, staged = _stage_cancel(session, series, scope, number=3)
    assert staged["disposition"] == "saved_with_errors", staged
    error = next(row for row in staged["diagnostic_sample"]
                 if row["code"] == "provider_event_binding_required")
    detail = error["details"]
    assert detail["category"] == "provider_readiness"
    assert detail["creation_operation_ref"] == create.ref_id
    assert detail["creation_operation_state"] == state
    assert detail["next_action"] == next_action
    assert detail["status_read"] == {
        "tool": "docket_get_history_entry",
        "arguments": {"ref": move_receipt["changeset_ref"], "view": "delivery"},
    }
    rejected = _commit(session, utterance2, trace2)
    assert rejected["disposition"] == "rejected_validation"
    draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == staged["draft_ref"]))
    request = session.scalar(select(SemanticRequest).where(
        SemanticRequest.ref_id == draft.semantic_request_ref,
    ))
    assert request.authority_availability == "available"
    assert staged["semantic_request_ref"] == rejected["semantic_request_ref"] == request.ref_id
    assert staged["authority_availability_at_operation"] == "available"
    assert rejected["authority_availability_at_operation"] == "available"
    assert draft.state == "draft" and draft.staged_actions_json
    assert session.scalar(select(func.count(Operation.id))) == before_operations
    assert session.scalar(select(func.count(CanonicalEvent.id))) == before_events

    # A subsequently completed projection is represented explicitly in this
    # isolated fixture. Retry the same semantic patch as a NEW stage operation.
    occurrence = session.scalar(select(EventOccurrence))
    master_binding = session.scalar(select(ProviderEventBinding))
    session.add(ProviderEventBinding(
        canonical_target_ref=occurrence.replacement_event_ref, target_kind="event",
        account_id=master_binding.account_id, calendar_id=master_binding.calendar_id,
        provider_event_id="recovered-child", status="active",
    ))
    session.flush()
    restaged = ChangeSetAssemblyService(session).stage(
        StageChangesInput(
            utterance_ref=utterance2.ref_id, request_key=utterance2.request_key,
            patch={"operations": [{"operation": "action_upsert", "action": action}
                                  for action in draft.staged_actions_json]},
        ),
        assembly_operation_token=_admit(session, utterance2, trace2, "stage_changes", 3),
        assembly_argument_hash="3" * 64,
    )
    assert restaged["disposition"] == "ready_to_commit", restaged
    assert restaged["draft_ref"] == draft.ref_id
    assert restaged["semantic_request_ref"] == request.ref_id
    receipt = ChangeSetAssemblyService(session).commit(
        utterance_ref=utterance2.ref_id, request_key=utterance2.request_key,
        assembly_operation_token=_admit(session, utterance2, trace2, "commit_changeset", 4),
        assembly_argument_hash="4" * 64,
    )
    assert receipt["disposition"] == "committed", receipt
    assert receipt["semantic_request_ref"] == request.ref_id
    assert occurrence.status == "cancelled" and series.status == "active"
    assert session.scalar(select(func.count(CanonicalEvent.id))) == before_events


def test_manual_event_preview_is_scoped_and_preserves_title_disagreement(session):
    series, _identity = _world(session)
    content = ChangeSetContent(
        basis_refs=series.basis_refs,
        event_changes=[CanonicalEventCreate(
            change_id="manual-series", action="create", object_type="canonical_event",
            affected_fields=["title", "event_spec"], basis_refs=series.basis_refs,
            create_spec={
                "title": "Canonical proposal title", "event_spec": series.event_spec,
                "lane_ref": series.lane_ref,
            },
        )],
    )
    snapshot = capture_event_preview(session, content)
    effect = snapshot["effects"][0]
    assert effect["before"] is None
    assert effect["after"]["title"] == "Canonical proposal title"
    assert effect["after"]["calendar_title"] == "MATH 1263"
    assert effect["scope"]["kind"] == "entire_series"
    sample = event_preview_sample(snapshot, entry_owned_ids=set(), budget=7000)
    assert sample["event_effect_count"] == 1
    assert sample["event_preview"][0]["title"] == "Canonical proposal title"
    # The same entry is never repeated as both normalized input and support Event.
    sample = event_preview_sample(snapshot, entry_owned_ids={"manual-series"}, budget=7000)
    assert sample["event_preview"] == []
    assert sample["event_effects_represented_by_entries"] == 1
    assert "basis_refs" not in json.dumps(snapshot)
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 1


def test_explicit_series_cancellation_includes_moved_child(session) -> None:
    series, identity = _world(session)
    replacement = {
        **series.event_spec,
        "recurrence": None,
        "timing": {
            "kind": "timed",
            "start_local": "2026-09-09T16:00:00",
            "end_local": "2026-09-09T16:50:00",
            "timezone": "America/Los_Angeles",
        },
    }
    utterance, trace, staged = _stage_cancel(
        session,
        series,
        {"kind": "occurrence", "identity": identity.model_dump(mode="json")},
        event_spec=replacement,
    )
    assert staged["assembly_ready"], staged
    assert _commit(session, utterance, trace)["disposition"] == "committed"
    occurrence = session.scalar(select(EventOccurrence))
    child = session.scalar(
        select(CanonicalEvent).where(CanonicalEvent.ref_id == occurrence.replacement_event_ref)
    )
    master_binding = session.scalar(select(ProviderEventBinding))
    session.add(
        ProviderEventBinding(
            canonical_target_ref=child.ref_id,
            target_kind="event",
            account_id=master_binding.account_id,
            calendar_id=master_binding.calendar_id,
            provider_event_id="delivered-child",
            status="active",
        )
    )
    session.flush()
    utterance2, trace2, staged2 = _stage_cancel(
        session,
        series,
        {"kind": "entire_series"},
        number=3,
    )
    assert staged2["assembly_ready"], staged2
    assert _commit(session, utterance2, trace2)["disposition"] == "committed"
    assert series.status == child.status == "cancelled"
    assert occurrence.status == "cancelled"


def test_relative_resolution_is_immutable_across_timezone_and_clock_changes(session) -> None:
    from docket.models import CalendarDateBinding
    from docket.services.event_occurrences import bind_calendar_date

    # Message instant is still September 7 in Los Angeles, already September 8 in Tokyo.
    utterance = _utterance(
        session, "Cancel class tomorrow", 1, said_at=datetime(2026, 9, 8, 3, tzinfo=UTC)
    )
    first = bind_calendar_date(
        session,
        utterance_ref=utterance.ref_id,
        timezone="America/Los_Angeles",
        relative_day="tomorrow",
    )
    retried = bind_calendar_date(
        session, utterance_ref=utterance.ref_id, timezone="Asia/Tokyo", relative_day="tomorrow"
    )
    assert first == retried
    assert retried.date == date(2026, 9, 8)
    assert retried.timezone == "America/Los_Angeles"
    assert session.scalar(select(func.count(CalendarDateBinding.id))) == 1


def test_occurrence_cancel_commits_exception_not_master_retraction(session) -> None:
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, staged = _stage_cancel(session, series, scope)
    assert staged["assembly_ready"], staged
    result = _commit(session, utterance, trace)
    assert result["disposition"] == "committed", result
    assert series.status == "active"
    assert series.event_spec["recurrence"]["excluded_dates"] == ["2026-09-07", "2026-09-08"]
    occurrence = session.scalar(select(EventOccurrence))
    assert occurrence is not None and occurrence.status == "cancelled"
    assert occurrence.original_local_date == date(2026, 9, 8)
    assert occurrence.original_timezone == "America/Los_Angeles"
    operations = list(session.scalars(select(Operation)))
    assert len(operations) == 1 and operations[0].operation_type == "calendar_update_event"
    utterance2, trace2, staged2 = _stage_cancel(session, series, scope, number=3)
    assert staged2["assembly_ready"], staged2
    result2 = _commit(session, utterance2, trace2)
    assert result2["disposition"] == "committed", result2
    assert session.scalar(select(func.count()).select_from(Operation)) == 1
    assert occurrence.version == 1


@pytest.mark.parametrize("mutation_type", ["canonical_event_cancel", "canonical_event_modify"])
def test_unscoped_recurring_master_mutation_is_blocked_in_handler(session, mutation_type) -> None:
    series, _identity = _world(session)
    changeset = ChangeSet(ref_id=new_public_ref("chg"), compiler_manifest_json={})
    values = dict(
        change_id="stale-recipe",
        object_type="canonical_event",
        object_ref=series.ref_id,
        affected_fields=["status"],
        basis_refs=series.basis_refs,
    )
    change = (
        CanonicalEventCancel(action="retract", **values)
        if mutation_type.endswith("cancel")
        else CanonicalEventModify(action="update", payload={"status": "cancelled"}, **values)
    )
    with pytest.raises(DocketError) as exc:
        CanonicalEventAuthorityService(session).apply_event(session, changeset, change)
    assert exc.value.code == "recurring_event_scope_required"
    assert series.status == "active"


def test_moved_occurrence_edits_and_cancellation_share_one_child(session) -> None:
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    replacement = {
        **series.event_spec,
        "recurrence": None,
        "timing": {
            "kind": "timed",
            "start_local": "2026-09-09T16:00:00",
            "end_local": "2026-09-09T16:50:00",
            "timezone": "America/Los_Angeles",
        },
    }
    utterance, trace, staged = _stage_cancel(session, series, scope, event_spec=replacement)
    assert staged["assembly_ready"], staged
    result = _commit(session, utterance, trace)
    assert result["disposition"] == "committed", result
    occurrence = session.scalar(select(EventOccurrence))
    child = session.scalar(
        select(CanonicalEvent).where(CanonicalEvent.ref_id == occurrence.replacement_event_ref)
    )
    assert child is not None and child.event_spec["timing"]["start_local"] == "2026-09-09T16:00:00"
    master_binding = session.scalar(select(ProviderEventBinding))
    session.add(
        ProviderEventBinding(
            canonical_target_ref=child.ref_id,
            target_kind="event",
            account_id=master_binding.account_id,
            calendar_id=master_binding.calendar_id,
            provider_event_id="replacement",
            status="active",
        )
    )
    session.flush()
    replacement2 = {**replacement, "title": "MATH 1263 — revised location", "location": "Room 121"}
    utterance2, trace2, staged2 = _stage_cancel(
        session, series, scope, number=3, event_spec=replacement2
    )
    assert staged2["assembly_ready"], staged2
    assert _commit(session, utterance2, trace2)["disposition"] == "committed"
    assert occurrence.replacement_event_ref == child.ref_id
    assert child.event_spec["location"] == "Room 121"
    utterance3, trace3, staged3 = _stage_cancel(session, series, scope, number=4)
    assert staged3["assembly_ready"], staged3
    assert _commit(session, utterance3, trace3)["disposition"] == "committed"
    assert child.status == "cancelled" and series.status == "active"
    assert occurrence.original_local_date == date(2026, 9, 8)
    assert occurrence.original_timezone == "America/Los_Angeles"
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 2

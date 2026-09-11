import hashlib
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
)
from docket.schemas.assembly import StageChangesInput
from docket.schemas.authority import CanonicalEventCancel, CanonicalEventModify
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.canonical_events import CanonicalEventAuthorityService
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
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
    settings = get_settings()
    return ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=trace,
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

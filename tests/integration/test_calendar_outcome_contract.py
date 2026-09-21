"""Resolved intent fixtures exercise staging/commit; they are not live-model evaluations."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from test_canonical_event_changesets import _capture
from test_changeset_assembly import _admit

from docket.domain.public_refs import new_public_ref
from docket.models import (
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Item,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.assembly import StageChangesInput
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.operations import OperationRunner


def _actions(utterance_ref, lane, outcome):
    def action(kind, mutation, change_id, spec):
        return {
            "operation": "action_upsert",
            "action": {
                "change_id": change_id, "mutation_type": mutation, "action": "create",
                "object_type": kind, "create_spec": spec,
                "affected_fields": list(spec), "basis_refs": [utterance_ref],
            },
        }

    if outcome == "meeting":
        return [
            action("canonical_event", "canonical_event_create", "meeting", {
                "title": "Drop-in meeting", "lane_ref": lane.ref_id,
                "event_spec": {
                    "title": "Drop-in meeting", "calendar_lane": lane.lane,
                    "timing": {
                        "kind": "timed", "start_local": "2026-09-21T14:00:00",
                        "end_local": "2026-09-21T14:30:00", "timezone": "America/Los_Angeles",
                    },
                    "location": "Office 121",
                },
            }),
            action("lane_routing_decision", "lane_routing_decision_create", "meeting-route", {
                "lane_ref": lane.ref_id, "event_change_id": "meeting",
                "decision_kind": "explicit_operator", "operator_confirmed": True,
            }),
        ]
    is_window = "window" in outcome
    time = {
        "kind": "datetime_interval", "start_local": "2026-09-21T14:00:00",
        "end_local": "2026-09-21T14:30:00", "timezone": "America/Los_Angeles",
    } if is_window else {
        "kind": "date", "date": "2026-09-21", "timezone": "America/Los_Angeles",
    }
    actions = [
        action("item", "item_create", "item", {"title": "Prepare notes"}),
        action("task", "task_create", "task", {
            "title": "Prepare notes", "item_change_id": "item",
        }),
        action("temporal_binding", "temporal_binding_create", "time", {
            "subject_change_id": "task", "role": "window" if is_window else "due_by",
            "temporal_value": time,
        }),
    ]
    if outcome.startswith("visible_"):
        actions.append(action(
            "temporal_calendar_projection", "temporal_calendar_projection_create", "display", {
                "temporal_binding_change_id": "time", "lane_ref": lane.ref_id,
                "display_policy": {
                    "kind": "interval_span" if is_window else "all_day_marker",
                    "transparency": "transparent",
                },
            },
        ))
    return actions


@pytest.mark.parametrize("outcome", [
    "meeting", "window", "deadline", "visible_window", "visible_deadline",
])
def test_resolved_outcome_stages_commits_and_reports_actual_calendar_effect(
    session_factory, outcome,
):
    expects_delivery = outcome == "meeting" or outcome.startswith("visible_")
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google", external_account_id="calendar-outcome-test",
            capabilities=["google_calendar"], enabled=True,
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id, lane="meetings", display_name="Meetings",
            color_hex="#039BE5", calendar_id="meetings@example.com", status="active",
            basis_refs=[new_public_ref("dec")], created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane)
        session.flush()
        messages = {
            "meeting": "I'm going to a drop-in meeting Monday from 2 to 2:30. Use Meetings.",
            "window": "I could prepare notes Monday 2 to 2:30; just track that option.",
            "deadline": "Prepare notes is due Monday September 21. Track it in Docket only.",
            "visible_window": (
                "Track preparing notes Monday 2 to 2:30 and show the window on Meetings."
            ),
            "visible_deadline": (
                "Notes are due September 21. Show that date on Meetings as a marker."
            ),
        }
        utterance_ref, request_key = _capture(
            session, message_id="1542899000000000501", text=messages[outcome],
        )
        utterance = session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id == utterance_ref,
        ))
        actions = _actions(utterance_ref, lane, outcome)
        request = StageChangesInput.model_validate({
            "utterance_ref": utterance_ref, "request_key": request_key,
            "assembly_scope": {
                "resolved_intent": {"kind": outcome},
                "allowed_mutation_types": [a["action"]["mutation_type"] for a in actions],
                "planned_create_types": [a["action"]["object_type"] for a in actions],
                "target_refs": [lane.ref_id] if expects_delivery else [],
            },
            "expected_versions": {lane.ref_id: lane.version} if expects_delivery else {},
            "patch": {"operations": actions},
        })
        service = ChangeSetAssemblyService(session)
        stage = service.stage(
            request,
            assembly_operation_token=_admit(
                session, utterance=utterance, trace_ref=trace_ref, call_id="stage", ordinal=1,
                tool_name="docket_stage_changes", argument_hash="a" * 64,
            ),
            assembly_argument_hash="a" * 64,
        )
        assert stage["disposition"] == "ready_to_commit", stage
        assert stage["predicted_provider_operation_count"] == int(expects_delivery)
        assert ("plans no Google" in stage["calendar_delivery_notice"]) is not expects_delivery
        assert session.scalar(select(func.count(Operation.id))) == 0
        # Optional review is deliberately omitted.
        receipt = service.commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=_admit(
                session, utterance=utterance, trace_ref=trace_ref, call_id="commit", ordinal=2,
                tool_name="docket_commit_changeset", argument_hash="b" * 64,
            ),
            assembly_argument_hash="b" * 64,
        )
        assert receipt["disposition"] == "committed", receipt
        assert receipt["provider_operation_count"] == int(expects_delivery)
        assert receipt["provider_disposition"] == (
            "queued" if expects_delivery else "no_provider_operations"
        )
        assert ("Docket only" in receipt["calendar_delivery_notice"]) is not expects_delivery
        assert session.scalar(select(func.count(CanonicalEvent.id))) == int(outcome == "meeting")
        assert session.scalar(select(func.count(TemporalCalendarProjection.id))) == int(
            outcome.startswith("visible_")
        )
        if outcome == "meeting":
            event = session.scalar(select(CanonicalEvent))
            assert event.title == "Drop-in meeting"
            assert event.lane_ref == lane.ref_id
            assert event.routing_decision_ref.startswith("route_")
            assert event.event_spec["timing"]["start_local"] == "2026-09-21T14:00:00"
            assert event.event_spec["timing"]["end_local"] == "2026-09-21T14:30:00"
            assert event.event_spec["location"] == "Office 121"
            assert session.scalar(select(func.count(Task.id))) == 0
            assert session.scalar(select(func.count(Item.id))) == 0
        else:
            binding = session.scalar(select(TemporalBinding))
            assert binding.role == ("window" if "window" in outcome else "due_by")
            assert session.scalar(select(Task)).task_state == "not_started"
            if "deadline" in outcome:
                assert binding.temporal_value == {
                    "kind": "date", "date": "2026-09-21", "timezone": "America/Los_Angeles",
                }
        original_receipt = deepcopy(session.scalar(select(ChangeSet)).commit_receipt_json)

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is expects_delivery
    assert len(provider.events) == int(expects_delivery)
    if expects_delivery:
        snapshot = next(iter(provider.events.values())).snapshot
        if "deadline" in outcome:
            assert snapshot["start"] == {"date": "2026-09-21"}
            assert snapshot["end"] == {"date": "2026-09-22"}
        else:
            assert snapshot["start"]["dateTime"] == "2026-09-21T14:00:00"
            assert snapshot["end"]["dateTime"] == "2026-09-21T14:30:00"
        assert snapshot["summary"] == (
            "Drop-in meeting" if outcome == "meeting" else "Prepare notes"
        )
    with session_factory.begin() as session:
        utterance = session.scalar(select(OperatorUtterance))
        replay = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=_admit(
                session, utterance=utterance, trace_ref=trace_ref, call_id="replay", ordinal=3,
                tool_name="docket_commit_changeset", argument_hash="c" * 64,
            ),
            assembly_argument_hash="c" * 64,
        )
        assert replay == original_receipt  # Historical queued notice is not live delivery status.
        assert session.scalar(select(func.count(Operation.id))) == int(expects_delivery)


def test_no_operation_notice_does_not_probe_oauth(monkeypatch):
    from docket.services.calendar_delivery_notice import calendar_delivery_notice

    refresh = Mock(side_effect=CalendarProviderError(
        "google_auth_invalid", "Invalid test credentials.", transient=False,
    ))
    monkeypatch.setattr(FakeCalendarProvider, "validate_authorization", refresh)
    assert "Docket only" in calendar_delivery_notice(0, phase="committed")
    refresh.assert_not_called()

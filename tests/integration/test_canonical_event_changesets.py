import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    Account,
    Approval,
    AttentionCase,
    AttentionCaseRevision,
    CalendarLane,
    CanonicalEvent,
    CaseItem,
    ChangeSet,
    Interaction,
    LaneRoutingDecision,
    Operation,
    ProviderEventBinding,
    SemanticRequestAttempt,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.authority import ChangeSetContent, IntentSessionOpen, StatementInput
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.operations import OperationRunner
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService


def _capture(session, *, message_id: str, text: str) -> tuple[str, str]:
    settings = get_settings()
    request_key = (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:0"
    )
    result = ProvenanceService(session).capture_operator_utterance(
        OperatorUtteranceCapture(
            request_id=uuid.uuid4(),
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id=message_id,
            actor_id=settings.operator_discord_user_id,
            verbatim_text=text,
            request_key=request_key,
        )
    )
    return str(result["ref"]), request_key


def _commit_rich_event(session, *, message_id: str) -> tuple[str, str, str]:
    account = Account(
        provider="google",
        external_account_id=f"event-changeset-{message_id}",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    utterance_ref, request_key = _capture(
        session,
        message_id=message_id,
        text="Create Clubs and put the PolyUAS meeting there Friday at 6 PM.",
    )
    lane_ref = new_public_ref("lane")
    statement_input = StatementInput.model_validate(
        {
            "statement_kind": "event_and_lane_command",
            "subject_refs": [lane_ref],
            "predicate": "create_event_in_lane",
            "value_json": {"event": "PolyUAS meeting", "lane": "Clubs"},
            "affected_fields": ["calendar_lane", "event", "route"],
            "interpreter_version": "event-changeset-test-v1",
        }
    )
    statement = StatementService(session).derive(utterance_ref, [statement_input])[0]
    basis = [statement.ref_id]
    content = ChangeSetContent.model_validate(
        {
            "basis_refs": basis,
            "lane_changes": [
                {
                    "change_id": "clubs-lane",
                    "action": "create",
                    "object_type": "calendar_lane",
                    "create_spec": {
                        "ref_id": lane_ref,
                        "account_id": str(account.id),
                        "name": "clubs",
                        "display_name": "Clubs",
                        "color_hex": "#0B8043",
                        "operator_policy_text": "Campus clubs and student teams.",
                    },
                    "affected_fields": ["lane", "provider_binding"],
                    "basis_refs": basis,
                },
                {
                    "change_id": "polyuas-route",
                    "action": "create",
                    "object_type": "lane_routing_decision",
                    "create_spec": {
                        "lane_change_id": "clubs-lane",
                        "event_change_id": "polyuas-event",
                        "organization_ref": None,
                        "recurring_identity": "polyuas-general-meeting",
                        "decision_kind": "explicit_operator",
                        "operator_confirmed": True,
                    },
                    "affected_fields": ["lane"],
                    "basis_refs": basis,
                },
            ],
            "event_changes": [
                {
                    "change_id": "polyuas-event",
                    "action": "create",
                    "object_type": "canonical_event",
                    "create_spec": {
                        "title": "PolyUAS general meeting",
                        "lane_change_id": "clubs-lane",
                        "event_spec": {
                            "title": "PolyUAS general meeting",
                            "calendar_lane": "clubs",
                            "timing": {
                                "kind": "timed",
                                "start_local": "2026-09-04T18:00:00",
                                "end_local": "2026-09-04T19:00:00",
                                "timezone": "America/Los_Angeles",
                            },
                            "location": "Cal Poly",
                            "operator_tags": ["club"],
                        },
                    },
                    "affected_fields": ["event", "lane", "time"],
                    "basis_refs": basis,
                }
            ],
        }
    )
    result = InteractiveAuthorityService(session).process_turn(
        utterance_ref=utterance_ref,
        request_key=request_key,
        actor_id=get_settings().operator_discord_user_id,
        intent_session_ref=None,
        expected_session_version=None,
        statements=[statement_input],
        relations=[],
        resolved_intent_json={"kind": "event_and_lane_authoring"},
        blocking_clarifications=[],
        content=content,
        changeset_ref=None,
        expected_changeset_version=None,
    )
    assert result["state"] == "committed"
    event = session.scalar(select(CanonicalEvent))
    lane = session.scalar(select(CalendarLane))
    changeset = session.scalar(select(ChangeSet))
    assert event is not None and lane is not None and changeset is not None
    return event.ref_id, lane.ref_id, changeset.ref_id


@pytest.mark.integration
def test_rich_event_changeset_commits_event_route_and_provider_intents_without_approval(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        event_ref, lane_ref, changeset_ref = _commit_rich_event(
            session, message_id="1542802000000000001"
        )
        event = session.scalar(select(CanonicalEvent).where(CanonicalEvent.ref_id == event_ref))
        route = session.scalar(select(LaneRoutingDecision))
        operations = list(session.scalars(select(Operation).order_by(Operation.created_at)))
        assert event is not None and event.provenance_status == "complete"
        assert event.lane_ref == lane_ref
        assert route is not None and route.event_ref == event.ref_id
        assert event.routing_decision_ref == route.ref_id
        assert [item.operation_type for item in operations] == [
            "calendar_configure_lane",
            "calendar_create_event",
        ]
        assert all(item.originating_changeset_ref == changeset_ref for item in operations)
        assert all(item.basis_refs for item in operations)
        assert all(item.canonical_target_refs for item in operations)
        assert all(item.provenance_status == "complete" for item in operations)
        assert operations[1].predecessor_operation_id == operations[0].id
        changeset = session.scalar(
            select(ChangeSet).where(ChangeSet.ref_id == changeset_ref)
        )
        assert changeset is not None
        assert {
            intent["operation_type"] for intent in changeset.provider_intents
        } == {"calendar_configure_lane", "calendar_create_event"}
        assert all(
            intent["parameters"].get("compiled_from_change_id")
            for intent in changeset.provider_intents
        )
        assert session.scalar(select(func.count(Approval.id))) == 0

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is True
    assert runner.run_due_once() is True
    with session_factory() as session:
        event = session.scalar(select(CanonicalEvent).where(CanonicalEvent.ref_id == event_ref))
        lane = session.scalar(select(CalendarLane).where(CalendarLane.ref_id == lane_ref))
        event_operation = session.scalar(
            select(Operation).where(Operation.operation_type == "calendar_create_event")
        )
        binding = session.scalar(select(ProviderEventBinding))
        assert event is not None and event.status == "active"
        assert lane is not None and lane.calendar_id == "fake-clubs"
        assert event_operation is not None and event_operation.status == "succeeded"
        assert binding is not None and binding.canonical_event_id == event.id
        assert session.scalar(select(func.count(Interaction.id))) == 0


@pytest.mark.integration
def test_package_pickup_case_reply_commits_event_route_and_resolution_first_try(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="package-pickup-account",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        utterance_ref, request_key = _capture(
            session,
            message_id="1542802000000000003",
            text="I'll grab it at 1 pm tomorrow, 15 minutes i guess",
        )
        lane = CalendarLane(
            account_id=account.id,
            lane="personal",
            display_name="Personal",
            color_hex="#8E24AA",
            calendar_id="personal@example.com",
            status="active",
            basis_refs=[utterance_ref],
            provenance_status="complete",
        )
        case = AttentionCase(
            situation_key="package-pickup-pacheco-post",
            title="Package ready for pickup at Pacheco Post",
            summary="A package is waiting at Pacheco Post Mail Center.",
            semantic_classes=["action_request"],
            first_observed_at=datetime(2026, 8, 28, 20, 23, tzinfo=UTC),
            last_observed_at=datetime(2026, 8, 28, 20, 23, tzinfo=UTC),
        )
        session.add_all([lane, case])
        session.flush()
        item = CaseItem(
            attention_case_id=case.id,
            item_key="pickup-decision",
            item_type="event_candidate",
            resolution_role="required",
            basis_refs=[utterance_ref],
        )
        session.add(item)
        session.flush()
        revision = AttentionCaseRevision(
            attention_case_id=case.id,
            case_ref=case.ref_id,
            revision=1,
            title=case.title,
            summary=case.summary,
            semantic_classes=list(case.semantic_classes),
            item_refs=[item.ref_id],
            source_refs=[],
            content_hash="4" * 64,
        )
        session.add(revision)
        session.flush()
        intent_session, created = IntentSessionService(session).open(
            IntentSessionOpen(
                source_utterance_ref=utterance_ref,
                case_refs=[case.ref_id],
                case_revision_refs=[revision.ref_id],
            )
        )
        assert created is True

        statement = StatementInput.model_validate(
            {
                "statement_kind": "package_pickup_plan",
                "subject_refs": [case.ref_id],
                "predicate": "pickup_time",
                "value_json": {
                    "start_local": "2026-08-29T13:00:00",
                    "duration_minutes": 15,
                },
                "affected_fields": ["event", "case_status"],
                "interpreter_version": "package-pickup-test-v1",
            }
        )
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": [utterance_ref],
                "expected_versions": {case.ref_id: case.version},
                "lane_changes": [
                    {
                        "mutation_type": "lane_routing_decision_create",
                        "change_id": "route-package-pickup-personal",
                        "action": "create",
                        "object_type": "lane_routing_decision",
                        "create_spec": {
                            "lane_ref": lane.ref_id,
                            "event_change_id": "create-package-pickup-event",
                            "recurring_identity": "pacheco-post-package-pickup",
                            "decision_kind": "explicit_operator",
                            "operator_confirmed": True,
                        },
                        "affected_fields": ["lane"],
                        "basis_refs": [utterance_ref],
                    }
                ],
                "event_changes": [
                    {
                        "mutation_type": "canonical_event_create",
                        "change_id": "create-package-pickup-event",
                        "action": "create",
                        "object_type": "canonical_event",
                        "create_spec": {
                            "title": "Pick up package at Pacheco Post",
                            "lane_ref": lane.ref_id,
                            "event_spec": {
                                "title": "Pick up package at Pacheco Post",
                                "calendar_lane": "personal",
                                "timing": {
                                    "kind": "timed",
                                    "start_local": "2026-08-29T13:00:00",
                                    "end_local": "2026-08-29T13:15:00",
                                    "timezone": "America/Los_Angeles",
                                },
                                "location": "Pacheco Post",
                                "reminder_plan": {
                                    "delivery_channels": ["google_popup"],
                                    "lead_seconds": [600],
                                },
                            },
                        },
                        "affected_fields": ["event", "lane", "time", "reminder"],
                        "basis_refs": [utterance_ref],
                    }
                ],
                "resolution_changes": [
                    {
                        "mutation_type": "attention_case_resolution",
                        "change_id": "resolve-package-pickup-case",
                        "action": "update",
                        "object_type": "attention_case_resolution",
                        "object_ref": case.ref_id,
                        "case_revision_ref": revision.ref_id,
                        "case_outcome": "resolved",
                        "item_dispositions": [
                            {"item_ref": item.ref_id, "disposition": "resolved"}
                        ],
                        "basis_refs": [utterance_ref],
                    }
                ],
            }
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=intent_session.ref_id,
            expected_session_version=intent_session.version,
            statements=[statement],
            relations=[],
            resolved_intent_json={"kind": "package_pickup_event"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )

        assert result["ok"] is True
        assert result["state"] == "committed"
        assert result["disposition"] == "committed"
        event = session.scalar(select(CanonicalEvent))
        route = session.scalar(select(LaneRoutingDecision))
        operation = session.scalar(select(Operation))
        changeset = session.scalar(select(ChangeSet))
        assert event is not None and route is not None and operation is not None
        assert changeset is not None
        assert event.routing_decision_ref == route.ref_id
        assert route.event_ref == event.ref_id
        assert case.status == "resolved"
        assert item.status == "resolved"
        assert operation.canonical_target_refs == [event.ref_id]
        assert len(changeset.provider_intents) == 1
        assert changeset.provider_intents[0]["operation_type"] == "calendar_create_event"
        assert changeset.provider_intents[0]["parameters"] == {
            "compiled_from_change_id": "create-package-pickup-event"
        }
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(func.count(SemanticRequestAttempt.id))) == 1


@pytest.mark.integration
def test_uncertain_provider_failure_does_not_roll_back_committed_event(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        event_ref, _lane_ref, changeset_ref = _commit_rich_event(
            session, message_id="1542802000000000002"
        )

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is True
    provider.next_create_outcome = "unknown_after_write"
    assert runner.run_due_once() is True
    with session_factory() as session:
        event = session.scalar(select(CanonicalEvent).where(CanonicalEvent.ref_id == event_ref))
        changeset = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == changeset_ref))
        operation = session.scalar(
            select(Operation).where(Operation.operation_type == "calendar_create_event")
        )
        assert event is not None and event.status == "active"
        assert changeset is not None and changeset.state == "committed"
        assert operation is not None and operation.status == "reconciliation_required"

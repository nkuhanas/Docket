import uuid

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    Account,
    CalendarLane,
    Entity,
    IdentityHandle,
    LaneRoutingDecision,
    Preference,
)
from docket.schemas.authority import ChangeSetContent, StatementInput
from docket.services.history import HistoryService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.policies import LaneRoutingService
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


@pytest.mark.integration
def test_ignore_unknown_sender_and_create_lane_route_commit_in_one_changeset(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="policy-lane-test",
            capabilities=["google_calendar", "gmail"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        utterance_ref, request_key = _capture(
            session,
            message_id="1542801000000000001",
            text=(
                "Ignore isaac@example.com from now on. Create Clubs and route this "
                "recurring PolyUAS meeting there."
            ),
        )
        lane_ref = new_public_ref("lane")
        statement_input = StatementInput.model_validate(
            {
                "statement_kind": "preference_and_route_command",
                "subject_refs": [lane_ref],
                "predicate": "configure_suppression_and_calendar_route",
                "value_json": {
                    "identity": "isaac@example.com",
                    "lane": "Clubs",
                },
                "affected_fields": ["preference", "calendar_lane", "route"],
                "interpreter_version": "policy-test-v1",
            }
        )
        statement = StatementService(session).derive(utterance_ref, [statement_input])[0]
        basis = [statement.ref_id]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": basis,
                "registry_changes": [
                    {
                        "change_id": "isaac-identity",
                        "action": "create",
                        "object_type": "identity_binding",
                        "create_spec": {
                            "handle_type": "email",
                            "value": "isaac@example.com",
                            "source_refs": [],
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    }
                ],
                "preference_changes": [
                    {
                        "change_id": "ignore-isaac",
                        "action": "create",
                        "object_type": "preference",
                        "create_spec": {
                            "preference_key": "gmail.ignore.isaac",
                            "policy_kind": "suppression",
                            "target_type": "identity",
                            "target_change_id": "isaac-identity",
                            "policy_text": "Ignore this sender from now on.",
                            "policy_json": {"disposition": "suppress"},
                        },
                        "affected_fields": ["suppression"],
                        "basis_refs": basis,
                    }
                ],
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
                            "operator_policy_text": "Extracurricular campus organizations.",
                            "metadata_json": {
                                "organization_types": ["student_club", "student_team"]
                            },
                        },
                        "affected_fields": ["lane", "policy"],
                        "basis_refs": basis,
                    },
                    {
                        "change_id": "polyuas-route",
                        "action": "create",
                        "object_type": "lane_routing_decision",
                        "create_spec": {
                            "lane_change_id": "clubs-lane",
                            "recurring_identity": "polyuas-general-meeting",
                            "decision_kind": "explicit_operator",
                            "operator_confirmed": True,
                        },
                        "affected_fields": ["lane"],
                        "basis_refs": basis,
                    },
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
            resolved_intent_json={"kind": "preference_and_lane_authoring"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed"
        handle = session.scalar(select(IdentityHandle))
        preference = session.scalar(select(Preference))
        lane = session.scalar(select(CalendarLane))
        route = session.scalar(select(LaneRoutingDecision))
        assert handle is not None and handle.status == "unbound"
        assert session.scalar(select(func.count(Entity.id))) == 0
        assert preference is not None and preference.target_ref == handle.ref_id
        assert lane is not None and lane.ref_id == lane_ref
        assert lane.operator_policy_text == "Extracurricular campus organizations."
        assert lane.provenance_status == "complete"
        assert route is not None and route.lane_ref == lane.ref_id
        assert route.operator_confirmed is True
        assert HistoryService(session).get_entry(preference.ref_id)["entry"][
            "basis_refs"
        ] == basis


@pytest.mark.integration
def test_lane_precedent_requires_three_latest_consistent_operator_decisions(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="precedent-test",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        clubs = CalendarLane(
            account_id=account.id,
            lane="clubs",
            display_name="Clubs",
            color_hex="#0B8043",
            status="active",
            calendar_id="clubs@example.com",
            enabled=True,
            metadata_json={"organization_types": ["student_club"]},
        )
        session.add(clubs)
        session.flush()
        for _ in range(2):
            session.add(
                LaneRoutingDecision(
                    lane_id=clubs.id,
                    lane_ref=clubs.ref_id,
                    organization_ref="ent_01M13MZZZZZZZZZZZZZZZZZZZZ",
                    recurring_identity="polyuas-general-meeting",
                    decision_kind="explicit_operator",
                    operator_confirmed=True,
                    created_by_changeset_ref=new_public_ref("chg"),
                )
            )
        session.flush()
        unresolved = LaneRoutingService(session).resolve(
            organization_ref="ent_01M13MZZZZZZZZZZZZZZZZZZZZ",
            semantic_metadata={"organization_type": "student_club"},
        )
        assert unresolved["state"] == "needs_clarification"
        assert unresolved["basis"] == "semantic_metadata_advisory"
        assert unresolved["suggested_lane_refs"] == [clubs.ref_id]

        session.add(
            LaneRoutingDecision(
                lane_id=clubs.id,
                lane_ref=clubs.ref_id,
                organization_ref="ent_01M13MZZZZZZZZZZZZZZZZZZZZ",
                recurring_identity="polyuas-2",
                decision_kind="explicit_operator",
                operator_confirmed=True,
                created_by_changeset_ref=new_public_ref("chg"),
            )
        )
        session.flush()
        resolved = LaneRoutingService(session).resolve(
            organization_ref="ent_01M13MZZZZZZZZZZZZZZZZZZZZ"
        )
        assert resolved["state"] == "resolved"
        assert resolved["lane_ref"] == clubs.ref_id
        assert resolved["basis"] == "historical_precedent"
        assert len(resolved["basis_refs"]) == 3
        assert session.scalar(select(func.count(Preference.id))) == 0


@pytest.mark.integration
def test_semantic_class_route_preference_applies_only_to_matching_class(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="semantic-route-test",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        clubs = CalendarLane(
            account_id=account.id,
            lane="clubs",
            display_name="Clubs",
            color_hex="#0B8043",
            status="active",
            calendar_id="clubs-semantic@example.com",
            enabled=True,
        )
        session.add(clubs)
        session.flush()
        preference = Preference(
            preference_key="calendar.route.event-invitation",
            policy_kind="calendar_route",
            target_type="semantic_class",
            semantic_class="event_invitation",
            policy_text="Route event invitations to Clubs.",
            policy_json={"lane_ref": clubs.ref_id},
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(preference)
        session.flush()

        unmatched = LaneRoutingService(session).resolve(
            semantic_classes={"informational"}
        )
        assert unmatched["state"] == "needs_clarification"
        matched = LaneRoutingService(session).resolve(
            semantic_classes={"event_invitation"}
        )
        assert matched == {
            "state": "resolved",
            "lane_ref": clubs.ref_id,
            "basis": "broad_structured_preference",
            "basis_refs": [preference.ref_id],
        }


@pytest.mark.integration
def test_lane_routing_precedence_distinguishes_exact_triage_and_broad_policy(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="route-precedence-test",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        clubs = CalendarLane(
            account_id=account.id,
            lane="clubs",
            display_name="Clubs",
            color_hex="#0B8043",
            status="active",
            calendar_id="clubs-precedence@example.com",
            enabled=True,
        )
        academic = CalendarLane(
            account_id=account.id,
            lane="academic",
            display_name="Academic",
            color_hex="#3F51B5",
            status="active",
            calendar_id="academic-precedence@example.com",
            enabled=True,
        )
        session.add_all([clubs, academic])
        session.flush()
        organization_ref = "ent_01M13MZZZZZZZZZZZZZZZZZZZZ"
        exact = Preference(
            preference_key="calendar.route.polyuas",
            policy_kind="calendar_route",
            target_type="entity",
            target_ref=organization_ref,
            policy_text="Route PolyUAS to Clubs.",
            policy_json={"lane_ref": clubs.ref_id},
            created_by_changeset_ref=new_public_ref("chg"),
        )
        broad = Preference(
            preference_key="calendar.route.default-events",
            policy_kind="calendar_route",
            target_type="semantic_class",
            semantic_class="event_invitation",
            policy_text="Route general invitations to Academic.",
            policy_json={"lane_ref": academic.ref_id},
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add_all([exact, broad])
        session.flush()
        routing = LaneRoutingService(session)

        explicit = routing.resolve(
            explicit_lane_ref=academic.ref_id,
            organization_ref=organization_ref,
            triage_policy_lane_ref=clubs.ref_id,
            semantic_classes={"event_invitation"},
        )
        assert explicit["lane_ref"] == academic.ref_id
        assert explicit["basis"] == "current_utterance"

        exact_result = routing.resolve(
            organization_ref=organization_ref,
            triage_policy_lane_ref=academic.ref_id,
            semantic_classes={"event_invitation"},
        )
        assert exact_result["lane_ref"] == clubs.ref_id
        assert exact_result["basis"] == "structured_preference"

        triage = routing.resolve(
            triage_policy_lane_ref=clubs.ref_id,
            semantic_classes={"event_invitation"},
        )
        assert triage["lane_ref"] == clubs.ref_id
        assert triage["basis"] == "triage_md"

        broad_result = routing.resolve(semantic_classes={"event_invitation"})
        assert broad_result["lane_ref"] == academic.ref_id
        assert broad_result["basis"] == "broad_structured_preference"

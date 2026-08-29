import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    Account,
    CalendarLane,
    ChangeSet,
    Entity,
    IdentityHandle,
    LaneRoutingDecision,
    Preference,
    SenderIdentityEmail,
)
from docket.schemas.authority import ChangeSetContent, PreferenceCreate, StatementInput
from docket.schemas.policy import PreferenceCreateSpec
from docket.schemas.registry import IdentityHandleCreateSpec
from docket.services.history import HistoryService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.policies import ContextPolicyService, LaneRoutingService
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService


def _capture(session, *, message_id: str, text: str) -> tuple[str, str]:
    settings = get_settings()
    request_key = f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
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


def test_sender_suppression_schema_requires_refs_and_executable_policy() -> None:
    identity_ref = new_public_ref("idn")
    sender_spec = IdentityHandleCreateSpec.model_validate(
        {"handle_type": "sender_label", "value": "Cal Poly Mustang Shop"}
    )
    assert sender_spec.associated_email_refs == []
    with pytest.raises(ValidationError):
        IdentityHandleCreateSpec.model_validate(
            {
                "handle_type": "email",
                "value": "shop@em.efollett.com",
                "associated_email_refs": [identity_ref],
            }
        )
    with pytest.raises(ValidationError):
        PreferenceCreateSpec.model_validate(
            {
                "preference_key": "gmail.ignore.label",
                "policy_kind": "suppression",
                "target_type": "identity",
                "target_key": "Cal Poly Mustang Shop",
                "policy_text": "Ignore this sender.",
                "policy_json": {"disposition": "suppress"},
            }
        )
    with pytest.raises(ValidationError):
        PreferenceCreateSpec.model_validate(
            {
                "preference_key": "gmail.ignore.inert",
                "policy_kind": "suppression",
                "target_type": "identity",
                "target_ref": identity_ref,
                "policy_text": "Ignore this sender.",
                "policy_json": {},
            }
        )


@pytest.mark.integration
def test_email_suppression_rejects_non_email_identity_target(session_factory) -> None:
    with session_factory.begin() as session:
        handle = IdentityHandle(
            handle_type="discord",
            value="mustang-shop",
            normalized_value="mustang-shop",
            status="unbound",
        )
        session.add(handle)
        session.flush()
        handle_ref = handle.ref_id

    with (
        pytest.raises(DocketError) as error,
        session_factory.begin() as session,
    ):
        ContextPolicyService(session).apply_preference(
            session,
            ChangeSet(
                id=uuid.uuid4(),
                ref_id=new_public_ref("chg"),
                intent_session_id=uuid.uuid4(),
                intent_session_ref=new_public_ref("ses"),
                idempotency_key="invalid-label-preference-test",
                basis_refs=[new_public_ref("utt")],
            ),
            PreferenceCreate.model_validate(
                {
                    "change_id": "invalid-label-preference",
                    "action": "create",
                    "object_type": "preference",
                    "create_spec": {
                        "preference_key": "gmail.ignore.invalid-label",
                        "policy_kind": "suppression",
                        "target_type": "identity",
                        "target_ref": handle_ref,
                        "policy_text": "Ignore this sender.",
                        "policy_json": {"disposition": "suppress"},
                    },
                    "affected_fields": ["suppression"],
                    "basis_refs": [new_public_ref("utt")],
                }
            ),
        )

    assert error.value.code == "unsupported_suppression_identity_type"
    with session_factory() as session:
        assert session.scalar(select(func.count(Preference.id))) == 0


@pytest.mark.integration
def test_sender_label_suppression_requires_associated_email(session_factory) -> None:
    with session_factory.begin() as session:
        handle = IdentityHandle(
            handle_type="sender_label",
            value="Cal Poly Mustang Shop",
            normalized_value="cal poly mustang shop",
            status="unbound",
        )
        session.add(handle)
        session.flush()
        handle_ref = handle.ref_id

    with (
        pytest.raises(DocketError) as error,
        session_factory.begin() as session,
    ):
        ContextPolicyService(session).apply_preference(
            session,
            ChangeSet(
                id=uuid.uuid4(),
                ref_id=new_public_ref("chg"),
                intent_session_id=uuid.uuid4(),
                intent_session_ref=new_public_ref("ses"),
                idempotency_key="unassociated-sender-preference-test",
                basis_refs=[new_public_ref("utt")],
            ),
            PreferenceCreate.model_validate(
                {
                    "change_id": "unassociated-sender-preference",
                    "action": "create",
                    "object_type": "preference",
                    "create_spec": {
                        "preference_key": "gmail.ignore.unassociated-sender",
                        "policy_kind": "suppression",
                        "target_type": "identity",
                        "target_ref": handle_ref,
                        "policy_text": "Ignore this sender.",
                        "policy_json": {"disposition": "suppress"},
                    },
                    "affected_fields": ["suppression"],
                    "basis_refs": [new_public_ref("utt")],
                }
            ),
        )

    assert error.value.code == "sender_identity_email_required"


@pytest.mark.integration
def test_sender_label_groups_exact_emails_and_owns_suppression_policy(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        utterance_ref, request_key = _capture(
            session,
            message_id="1542801000000000002",
            text="Suppress everything from the Cal Poly Mustang Shop sender.",
        )
        statement_input = StatementInput.model_validate(
            {
                "statement_kind": "sender_suppression_command",
                "subject_refs": [utterance_ref],
                "predicate": "suppress_email_sender",
                "value_json": {
                    "label": "Cal Poly Mustang Shop",
                    "email": "shop@em.efollett.com",
                },
                "affected_fields": ["identity", "preference"],
                "interpreter_version": "sender-policy-test-v1",
            }
        )
        statement = StatementService(session).derive(utterance_ref, [statement_input])[0]
        basis = [statement.ref_id]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": basis,
                "registry_changes": [
                    {
                        "change_id": "mustang-email",
                        "action": "create",
                        "object_type": "identity_binding",
                        "create_spec": {
                            "handle_type": "email",
                            "value": "shop@em.efollett.com",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    },
                    {
                        "change_id": "mustang-sender",
                        "action": "create",
                        "object_type": "identity_binding",
                        "create_spec": {
                            "handle_type": "sender_label",
                            "value": "Cal Poly Mustang Shop",
                            "associated_email_change_ids": ["mustang-email"],
                        },
                        "affected_fields": ["identity", "associated_emails"],
                        "basis_refs": basis,
                    },
                ],
                "preference_changes": [
                    {
                        "change_id": "mustang-suppression",
                        "action": "create",
                        "object_type": "preference",
                        "create_spec": {
                            "preference_key": "gmail.ignore.cal-poly-mustang-shop",
                            "policy_kind": "suppression",
                            "target_type": "identity",
                            "target_change_id": "mustang-sender",
                            "policy_text": "Suppress all email triage from this sender.",
                            "policy_json": {"disposition": "suppress"},
                        },
                        "affected_fields": ["suppression"],
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
            resolved_intent_json={"kind": "sender_suppression"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed"
        handles = list(session.scalars(select(IdentityHandle).order_by(IdentityHandle.handle_type)))
        email = next(item for item in handles if item.handle_type == "email")
        sender = next(item for item in handles if item.handle_type == "sender_label")
        association = session.scalar(select(SenderIdentityEmail))
        preference = session.scalar(select(Preference))
        assert association is not None and association.status == "active"
        assert association.sender_identity_handle_id == sender.id
        assert association.email_identity_handle_id == email.id
        assert association.basis_refs == basis
        assert preference is not None and preference.target_ref == sender.ref_id
        sender_entry = HistoryService(session).get_entry(sender.ref_id)["entry"]
        assert sender_entry["associated_emails"] == [
            {
                "identity_ref": email.ref_id,
                "value": "shop@em.efollett.com",
                "identity_status": "unbound",
                "association_status": "active",
                "valid_from": association.valid_from.isoformat(),
                "valid_to": None,
            }
        ]
        preference_entry = HistoryService(session).get_entry(preference.ref_id)["entry"]
        assert preference_entry["target_ref"] == sender.ref_id
        assert preference_entry["policy_json"] == {"disposition": "suppress"}


@pytest.mark.integration
def test_existing_label_and_inert_preference_are_corrected_atomically(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        sender = IdentityHandle(
            handle_type="sender_label",
            value="Cal Poly Mustang Shop",
            normalized_value="cal poly mustang shop",
            status="unbound",
        )
        session.add(sender)
        session.flush()
        preference = Preference(
            preference_key="email_triage.sender.cal_poly_mustang_shop.suppress_all",
            policy_kind="suppression",
            target_type="identity",
            target_ref=sender.ref_id,
            policy_text="Suppress everything from this sender.",
            policy_json={},
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(preference)
        session.flush()
        sender_ref = sender.ref_id
        preference_ref = preference.ref_id
        utterance_ref, request_key = _capture(
            session,
            message_id="1542801000000000003",
            text=(
                "Correct the Mustang Shop rule: associate shop@em.efollett.com "
                "and make the preference suppress it."
            ),
        )
        statement_input = StatementInput.model_validate(
            {
                "statement_kind": "sender_suppression_correction",
                "subject_refs": [sender_ref, preference_ref],
                "predicate": "correct_sender_suppression",
                "value_json": {"email": "shop@em.efollett.com"},
                "affected_fields": ["associated_emails", "policy_json"],
                "interpreter_version": "sender-policy-test-v1",
            }
        )
        statement = StatementService(session).derive(utterance_ref, [statement_input])[0]
        basis = [statement.ref_id]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": basis,
                "expected_versions": {sender_ref: 1, preference_ref: 1},
                "registry_changes": [
                    {
                        "change_id": "mustang-email",
                        "action": "create",
                        "object_type": "identity_binding",
                        "create_spec": {
                            "handle_type": "email",
                            "value": "shop@em.efollett.com",
                        },
                        "affected_fields": ["identity"],
                        "basis_refs": basis,
                    },
                    {
                        "change_id": "associate-mustang-email",
                        "action": "update",
                        "object_type": "identity_binding",
                        "object_ref": sender_ref,
                        "payload": {"add_associated_email_change_id": "mustang-email"},
                        "affected_fields": ["associated_emails"],
                        "basis_refs": basis,
                    },
                ],
                "preference_changes": [
                    {
                        "change_id": "activate-mustang-suppression",
                        "action": "update",
                        "object_type": "preference",
                        "object_ref": preference_ref,
                        "payload": {"policy_json": {"disposition": "suppress"}},
                        "affected_fields": ["policy_json"],
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
            resolved_intent_json={"kind": "sender_suppression_correction"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed"
        association = session.scalar(select(SenderIdentityEmail))
        stored_preference = session.scalar(
            select(Preference).where(Preference.ref_id == preference_ref)
        )
        assert association is not None and association.status == "active"
        assert stored_preference is not None
        assert stored_preference.policy_json == {"disposition": "suppress"}
        assert stored_preference.version == 2


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
        stored_preference = HistoryService(session).get_entry(preference.ref_id)["entry"]
        assert stored_preference["basis_refs"] == basis
        assert stored_preference["target_ref"] == handle.ref_id
        assert stored_preference["policy_json"] == {"disposition": "suppress"}
        assert stored_preference["scope_json"] == {}


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

        unmatched = LaneRoutingService(session).resolve(semantic_classes={"informational"})
        assert unmatched["state"] == "needs_clarification"
        matched = LaneRoutingService(session).resolve(semantic_classes={"event_invitation"})
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

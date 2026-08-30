from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    CalendarLane,
    ChangeSet,
    Item,
    Operation,
    ProviderAccount,
    ProviderEventBinding,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.authority import ChangeSetContent
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.operations import OperationRunner
from docket.services.provenance import ProvenanceService
from docket.services.tracked_context import TrackedContextService


@pytest.mark.integration
def test_time_allows_only_one_active_calendar_projection(session) -> None:
    account = ProviderAccount(
        provider="google",
        external_account_id="single-time-projection",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    lanes = [
        CalendarLane(
            account_id=account.id,
            lane=slug,
            display_name=slug.title(),
            color_hex=color,
            calendar_id=f"{slug}@example.com",
            status="active",
            basis_refs=[new_public_ref("dec")],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        for slug, color in (("academics", "#039BE5"), ("personal", "#8E24AA"))
    ]
    session.add_all(lanes)
    item = Item(
        title="Midterm",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(item)
    session.flush()
    binding = TemporalBinding(
        subject_ref=item.ref_id,
        role="scheduled_on",
        temporal_value={
            "kind": "date",
            "date": "2026-09-18",
            "timezone": "America/Los_Angeles",
        },
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(binding)
    session.flush()
    changeset = ChangeSet(
        intent_session_id=uuid.uuid4(),
        intent_session_ref=new_public_ref("ses"),
        idempotency_key="test:single-time-projection",
        basis_refs=[new_public_ref("utt")],
        state="validated",
    )
    session.add(changeset)
    session.flush()
    changes = ChangeSetContent.model_validate(
        {
            "basis_refs": changeset.basis_refs,
            "tracked_context_changes": [
                {
                    "mutation_type": "temporal_calendar_projection_create",
                    "change_id": f"projection-{lane.lane}",
                    "action": "create",
                    "object_type": "temporal_calendar_projection",
                    "affected_fields": ["lane_ref"],
                    "basis_refs": changeset.basis_refs,
                    "create_spec": {
                        "temporal_binding_ref": binding.ref_id,
                        "lane_ref": lane.ref_id,
                        "display_policy": {
                            "kind": "all_day_marker",
                            "transparency": "transparent",
                        },
                    },
                }
                for lane in lanes
            ],
        }
    ).tracked_context_changes
    service = TrackedContextService(session)
    service.apply_temporal_projection(session, changeset, changes[0])
    with pytest.raises(DocketError) as error:
        service.apply_temporal_projection(session, changeset, changes[1])
    assert error.value.code == "temporal_projection_exists"


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
def test_google_popup_reminder_without_calendar_projection_is_rejected(
    session,
) -> None:
    utterance_ref, request_key = _capture(
        session,
        message_id="1542803000000000000",
        text="Remind me one day before the midterm with a Google popup.",
    )
    item = Item(
        title="MATH 1263 Midterm",
        basis_refs=[utterance_ref],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(item)
    session.flush()
    binding = TemporalBinding(
        subject_ref=item.ref_id,
        role="scheduled_on",
        temporal_value={
            "kind": "date",
            "date": "2026-09-18",
            "timezone": "America/Los_Angeles",
        },
        basis_refs=[utterance_ref],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=new_public_ref("chg"),
    )
    session.add(binding)
    session.flush()
    content = ChangeSetContent.model_validate(
        {
            "basis_refs": [utterance_ref],
            "tracked_context_changes": [
                {
                    "mutation_type": "reminder_plan_create",
                    "change_id": "unprojected-midterm-reminder",
                    "action": "create",
                    "object_type": "reminder_plan",
                    "affected_fields": ["delivery_channels", "lead_seconds"],
                    "basis_refs": [utterance_ref],
                    "create_spec": {
                        "subject_ref": binding.ref_id,
                        "delivery_channels": ["google_popup"],
                        "lead_seconds": [86400],
                        "date_trigger_local_time": "09:00:00",
                        "timezone": "America/Los_Angeles",
                    },
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
        statements=[],
        relations=[],
        resolved_intent_json={"kind": "reminder_plan"},
        blocking_clarifications=[],
        content=content,
        changeset_ref=None,
        expected_changeset_version=None,
    )

    assert result["disposition"] == "rejected_validation"
    assert any(
        error["code"] == "google_popup_projection_required"
        for error in result["changeset"]["validation_errors"]
    )


@pytest.mark.integration
def test_time_projection_compiles_and_executes_provider_operation(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="time-projection-test",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="academics",
            display_name="Academics",
            color_hex="#039BE5",
            calendar_id="academics@example.com",
            status="active",
            basis_refs=[new_public_ref("dec")],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane)
        utterance_ref, request_key = _capture(
            session,
            message_id="1542803000000000001",
            text="Show the MATH 1263 midterm on Academics as a transparent marker.",
        )
        item = Item(
            title="MATH 1263 Midterm",
            kind="academic.exam",
            basis_refs=[utterance_ref],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(item)
        session.flush()
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": [utterance_ref],
                "tracked_context_changes": [
                    {
                        "mutation_type": "temporal_binding_create",
                        "change_id": "midterm-date",
                        "action": "create",
                        "object_type": "temporal_binding",
                        "affected_fields": ["temporal_value"],
                        "basis_refs": [utterance_ref],
                        "create_spec": {
                            "subject_ref": item.ref_id,
                            "role": "scheduled_on",
                            "temporal_value": {
                                "kind": "date",
                                "date": "2026-09-18",
                                "timezone": "America/Los_Angeles",
                            },
                        },
                    },
                    {
                        "mutation_type": "reminder_plan_create",
                        "change_id": "midterm-reminder",
                        "action": "create",
                        "object_type": "reminder_plan",
                        "affected_fields": ["delivery_channels", "lead_seconds"],
                        "basis_refs": [utterance_ref],
                        "create_spec": {
                            "subject_change_id": "midterm-date",
                            "delivery_channels": ["google_popup"],
                            "lead_seconds": [86400],
                            "date_trigger_local_time": "09:00:00",
                            "timezone": "America/Los_Angeles",
                        },
                    },
                    {
                        "mutation_type": "temporal_calendar_projection_create",
                        "change_id": "midterm-calendar-marker",
                        "action": "create",
                        "object_type": "temporal_calendar_projection",
                        "affected_fields": ["display_policy", "lane_ref"],
                        "basis_refs": [utterance_ref],
                        "create_spec": {
                            "temporal_binding_change_id": "midterm-date",
                            "lane_ref": lane.ref_id,
                            "reminder_plan_change_id": "midterm-reminder",
                            "display_policy": {
                                "kind": "all_day_marker",
                                "transparency": "transparent",
                            },
                        },
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
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "temporal_calendar_projection"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed", result
        binding = session.scalar(select(TemporalBinding))
        projection = session.scalar(select(TemporalCalendarProjection))
        operation = session.scalar(
            select(Operation).where(
                Operation.operation_type == "calendar_create_event"
            )
        )
        assert binding is not None and projection is not None and operation is not None
        assert operation.canonical_target_refs == [projection.ref_id]

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is True

    with session_factory() as session:
        projection = session.scalar(select(TemporalCalendarProjection))
        provider_binding = session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.target_kind == "temporal_projection"
            )
        )
        assert projection is not None and provider_binding is not None
        assert provider_binding.canonical_target_ref == projection.ref_id
        snapshot = provider_binding.provider_snapshot
        assert snapshot["summary"] == "MATH 1263 Midterm"
        assert snapshot["start"] == {"date": "2026-09-18"}
        assert snapshot["end"] == {"date": "2026-09-19"}
        assert snapshot["transparency"] == "transparent"
        assert snapshot["reminders"] == {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 1440}],
        }

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.models import (
    Item,
    OperatorProjection,
    OutboxEvent,
    ProjectionDelivery,
    ReminderPlan,
    ScheduledNotification,
    TemporalBinding,
)
from docket.services.reminders import ReminderService


@pytest.mark.integration
def test_date_reminder_projects_once_and_reconciles_delivery(session_factory) -> None:
    now = datetime(2026, 9, 1, 8, 5, tzinfo=UTC)
    with session_factory.begin() as session:
        item = Item(
            title="Submit application",
            kind="application",
            context_entity_refs=[],
            metadata_json={},
            basis_refs=["utt_01M16REMINDER0000000000000"],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref="chg_01M16REMINDER0000000000000",
        )
        session.add(item)
        session.flush()
        binding = TemporalBinding(
            subject_ref=item.ref_id,
            role="due_by",
            binding_key="application-deadline",
            temporal_value={
                "kind": "date",
                "date": "2026-09-01",
                "timezone": "UTC",
            },
            basis_refs=item.basis_refs,
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=item.created_by_changeset_ref,
        )
        session.add(binding)
        session.flush()
        plan = ReminderPlan(
            subject_ref=binding.ref_id,
            delivery_channels=["docket_queue"],
            lead_seconds=[3600],
            date_trigger_local_time="09:00:00",
            timezone="UTC",
            basis_refs=item.basis_refs,
            created_by_changeset_ref=item.created_by_changeset_ref,
        )
        session.add(plan)
        session.flush()
        plan_ref = plan.ref_id

    service = ReminderService(
        session_factory,
        get_settings().model_copy(update={"timezone": "UTC"}),
        clock=lambda: now,
    )
    assert service.run_due_once() is True
    with session_factory.begin() as session:
        notification = session.scalar(select(ScheduledNotification))
        projection = session.scalar(select(OperatorProjection))
        delivery = session.scalar(select(ProjectionDelivery))
        outbox = session.scalar(select(OutboxEvent))
        assert notification is not None and notification.status == "delivering"
        assert projection is not None and projection.primary_public_ref == plan_ref
        assert projection.projection_kind == "reminder"
        assert delivery is not None and delivery.projection_ref == projection.ref_id
        assert outbox is not None and outbox.payload == {"projection_ref": projection.ref_id}
        outbox.status = "delivered"

    assert service.run_due_once() is True
    assert service.run_due_once() is False
    with session_factory() as session:
        notification = session.scalar(select(ScheduledNotification))
        assert notification is not None and notification.status == "delivered"
        assert session.scalar(select(func.count(OperatorProjection.id))) == 1
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1

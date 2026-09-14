"""PostgreSQL recovery races; run only in the isolated Compose smoke database."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import func, select

from docket.config import get_settings
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    IntentSession,
    Operation,
    OperationTarget,
    ProviderAccount,
)
from docket.models.base import utc_now
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.authority import ProviderIntentInput
from docket.services.operations import OperationRunner
from docket.services.provenance import ProvenanceService
from docket.services.provider_intents import ProviderIntentService

CREDENTIAL_REF = "/run/smoke-only/google-reauth.json"


def _committed_delivery(factory, message_id):
    key = f"reauth-smoke-{message_id}"
    settings = get_settings()
    with factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(OperatorUtteranceCapture(
            request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id, message_id=message_id,
            actor_id=settings.operator_discord_user_id,
            verbatim_text="Add the smoke meeting to my calendar.",
            request_key=(
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
            ),
        ))
        basis = [str(captured["ref"])]
        intent_session = IntentSession(
            conversation_ref=key, source_utterance_ref=basis[0], semantic_state="ready",
            commit_state="committed",
        )
        account = ProviderAccount(
            provider="google", external_account_id=key, enabled=True,
            capabilities=["google_calendar"], credential_ref=CREDENTIAL_REF,
        )
        session.add_all([intent_session, account])
        session.flush()
        changeset = ChangeSet(
            intent_session_id=intent_session.id, intent_session_ref=intent_session.ref_id,
            idempotency_key=key, basis_refs=basis, state="committed", committed_at=utc_now(),
        )
        session.add(changeset)
        session.flush()
        lane = CalendarLane(
            account_id=account.id, lane=key, display_name="Smoke", calendar_id=f"{key}@example.com",
            color_hex="#3367D6", status="active", basis_refs=basis,
            created_by_changeset_ref=changeset.ref_id,
        )
        session.add(lane)
        session.flush()
        event = CanonicalEvent(
            canonical_key=key, title="Smoke meeting", status="active",
            authority="explicit_operator", lane_ref=lane.ref_id, lane_id=lane.id,
            event_spec={"title": "Smoke meeting", "calendar_lane": lane.lane, "timing": {
                "kind": "timed", "start_local": "2026-09-18T09:00:00",
                "end_local": "2026-09-18T10:00:00", "timezone": "America/Los_Angeles",
            }}, basis_refs=basis, created_by_changeset_ref=changeset.ref_id,
        )
        session.add(event)
        session.flush()
        refs = ProviderIntentService(session).materialize(session, changeset, ProviderIntentInput(
            intent_id="deliver", operation_type="calendar_create_event", account_ref=account.ref_id,
            canonical_target_refs=[event.ref_id], basis_refs=basis,
            idempotency_key=f"{key}:delivery",
            parameters={},
        ), {})
        session.flush()
        operation = session.scalar(select(Operation).where(Operation.ref_id == refs[0]))
        target = session.scalar(select(OperationTarget).where(
            OperationTarget.operation_id == operation.id,
        ))
        operation.status = target.status = "failed"
        operation.last_error_code = target.last_error_code = "google_auth_invalid"
        return account.external_account_id, operation.id, event.id, target.parameters_sha256


def test_google_reauthorization_recovery_serializes_and_preserves_delivery(factory) -> None:
    account, operation_id, _event_id, payload_hash = _committed_delivery(
        factory, "1542799000000000981",
    )
    admitted = threading.Barrier(2)

    def recover():
        provider = FakeCalendarProvider()
        with patch.object(provider, "validate_authorization", lambda: admitted.wait(timeout=10)):
            return OperationRunner(factory, provider).requeue_after_reauthorization(
                external_account_id=account, credential_ref=CREDENTIAL_REF, batch_size=1,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(recover), pool.submit(recover)
        receipts = [first.result(timeout=20), second.result(timeout=20)]
    assert sum(receipt.requeued for receipt in receipts) == 1
    with factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation.status == "pending"
        target = session.scalar(select(OperationTarget).where(
            OperationTarget.operation_id == operation_id,
        ))
        assert target.status == "pending" and target.parameters_sha256 == payload_hash
        assert target.parameters["external_event_id"] == operation_id.hex
        assert session.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.primary_ref == operation.ref_id,
            AuditEvent.event_type == "operation.requeued_after_auth_restore",
        )) == 1


def test_google_reauthorization_rechecks_a_concurrent_canonical_edit(factory) -> None:
    account, operation_id, event_id, _hash = _committed_delivery(factory, "1542799000000000982")
    checking = threading.Event()
    original = ProviderIntentService.auth_recovery_blocker

    def blocker(service, operation, target):
        checking.set()
        return original(service, operation, target)

    runner = OperationRunner(factory, FakeCalendarProvider())
    with (
        patch.object(ProviderIntentService, "auth_recovery_blocker", blocker),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with factory.begin() as session:
            event = session.scalar(select(CanonicalEvent).where(
                CanonicalEvent.id == event_id,
            ).with_for_update())
            future = pool.submit(
                runner.requeue_after_reauthorization,
                external_account_id=account, credential_ref=CREDENTIAL_REF,
            )
            assert checking.wait(timeout=10)
            assert not future.done()
            event.title = "A newer committed meeting title"
        receipt = future.result(timeout=20)
    assert receipt.requeued == 0
    assert receipt.skipped == {"canonical_or_provider_state_changed": 1}
    with factory() as session:
        assert session.get(Operation, operation_id).status == "failed"

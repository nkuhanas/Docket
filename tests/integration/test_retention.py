import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Account,
    Action,
    ActionRevision,
    AuditEvent,
    Operation,
    OperationItem,
    QueueItem,
    QueueItemSource,
    Record,
    SourceItem,
)
from docket.services.retention import RetentionService


@pytest.mark.integration
def test_retention_prunes_only_unreferenced_expired_source_metadata(
    session_factory,
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="retention-test",
            capabilities=["gmail"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        protected = SourceItem(
            account_id=account.id,
            provider="gmail",
            external_object_id="protected",
            source_version="1",
            source_fingerprint="a" * 64,
            minimal_headers={},
            status="ignored",
            created_at=now - timedelta(days=45),
        )
        expired_ignored = SourceItem(
            account_id=account.id,
            provider="gmail",
            external_object_id="expired-ignored",
            source_version="1",
            source_fingerprint="b" * 64,
            minimal_headers={},
            status="ignored",
            created_at=now - timedelta(days=45),
        )
        expired_ordinary = SourceItem(
            account_id=account.id,
            provider="gmail",
            external_object_id="expired-ordinary",
            source_version="1",
            source_fingerprint="c" * 64,
            minimal_headers={},
            status="failed",
            created_at=now - timedelta(days=400),
        )
        session.add_all([protected, expired_ignored, expired_ordinary])
        session.flush()
        queue_item = QueueItem(
            primary_source_item_id=protected.id,
            deduplication_key=f"retention:{uuid.uuid4()}",
            material_fingerprint="d" * 64,
            category="general_action",
            title="Protected queue source",
            summary="Referenced metadata must remain.",
            status="completed",
            priority="normal",
        )
        session.add(queue_item)
        session.flush()
        session.add(
            QueueItemSource(
                queue_item_id=queue_item.id,
                source_item_id=protected.id,
                relationship="primary",
            )
        )
        session.add(
            AuditEvent(
                event_type="expired.audit",
                entity_type="test",
                entity_id=None,
                actor_type="system",
                actor_id=None,
                data={},
                created_at=now - timedelta(days=400),
            )
        )
        active_record = Record(
            record_type="course",
            canonical_key="retention-active-course",
            title="Active retention course",
            status="active",
            data={},
            updated_at=now - timedelta(days=400),
        )
        archived_record = Record(
            record_type="course",
            canonical_key="retention-archived-course",
            title="Archived retention course",
            status="archived",
            data={},
            updated_at=now - timedelta(days=400),
        )
        session.add_all([active_record, archived_record])
        session.flush()

        operation_items: list[OperationItem] = []
        for label, record in (
            ("active", active_record),
            ("archived", archived_record),
        ):
            action = Action(
                record_id=record.id,
                action_type="calendar_reconcile_course",
                status="succeeded",
            )
            session.add(action)
            session.flush()
            revision = ActionRevision(
                action_id=action.id,
                revision=1,
                action_type=action.action_type,
                account_id=account.id,
                parameters={"record_id": str(record.id)},
                parameters_sha256=label[0] * 64,
                preview={},
                preview_sha256=label[-1] * 64,
                risk_class="bulk",
                target_versions={"record": 1},
                created_by_actor_type="system",
            )
            session.add(revision)
            session.flush()
            operation = Operation(
                action_revision_id=revision.id,
                idempotency_key=f"retention-operation-{label}",
                operation_type="calendar_reconcile_course",
                account_id=account.id,
                status="succeeded",
                provider_correlation=f"retention-operation-{label}",
            )
            session.add(operation)
            session.flush()
            item = OperationItem(
                operation_id=operation.id,
                item_key=f"retention-item-{label}",
                item_type="calendar_create_event",
                idempotency_key=f"retention-item-{label}",
                parameters={"record_id": str(record.id)},
                parameters_sha256="0" * 64,
                status="succeeded",
                result={"provider_event_id": f"event-{label}"},
                updated_at=now - timedelta(days=400),
            )
            session.add(item)
            operation_items.append(item)

    settings = get_settings().model_copy(update={"retention_enabled": True})
    service = RetentionService(session_factory, settings)
    result = service.run_due_once(force=True)

    assert result.ran
    assert result.counts["ignored_sources"] == 1
    assert result.counts["ordinary_sources"] == 1
    assert result.counts["audits"] == 1
    assert result.counts["scrubbed_operation_item_results"] == 1
    with session_factory() as session:
        sources = session.scalars(select(SourceItem)).all()
        assert [source.external_object_id for source in sources] == ["protected"]
        cleanups = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "retention.cleanup_completed"
            )
        ).all()
        assert len(cleanups) == 1
        retained_results = {
            item.item_key: item.result
            for item in session.scalars(select(OperationItem)).all()
        }
        assert retained_results["retention-item-active"] is not None
        assert retained_results["retention-item-archived"] is None

    assert not service.run_due_once().ran

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    CommandStatus,
    OperationStatus,
    OutboxStatus,
    QueueItemStatus,
)
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    Action,
    ActionRevision,
    AuditEvent,
    CommandRequest,
    ExecutionAttempt,
    Operation,
    OutboxEvent,
    QueueItem,
)
from docket.models.base import utc_now
from docket.policy import GMAIL_MUTATION_ACTION_TYPES


class GmailRecoveryService:
    """Promote a known post-call Gmail parse failure into read-only reconciliation."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _finish(command: CommandRequest, result: dict[str, Any]) -> None:
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = utc_now()

    def request_reconciliation(
        self,
        *,
        operation_id: uuid.UUID,
        request_key: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not request_key or len(request_key) > 512:
            raise DocketError(
                code="invalid_request_key",
                message="The recovery request key is invalid.",
            )
        payload = {
            "operation_id": str(operation_id),
            "actor_id": actor_id,
        }
        operation_name = "docket_request_gmail_reconciliation"
        input_sha256 = sha256_json(payload)
        with self.session_factory.begin() as session:
            existing_command = session.scalar(
                select(CommandRequest)
                .where(CommandRequest.request_key == request_key)
                .with_for_update()
            )
            if existing_command is not None:
                if (
                    existing_command.operation_name != operation_name
                    or existing_command.input_sha256 != input_sha256
                ):
                    raise IdempotencyConflict(
                        request_key,
                        existing_operation=existing_command.operation_name,
                        attempted_operation=operation_name,
                    )
                if (
                    existing_command.status == CommandStatus.SUCCEEDED.value
                    and existing_command.result is not None
                ):
                    replay = dict(existing_command.result)
                    replay["disposition"] = "replayed_request"
                    return replay
                raise DocketError(
                    code="request_in_progress",
                    message="The Gmail recovery request has not completed.",
                )
            command = CommandRequest(
                request_key=request_key,
                operation_name=operation_name,
                input_sha256=input_sha256,
                actor_type="operator",
                actor_id=actor_id,
                status=CommandStatus.IN_PROGRESS.value,
            )
            session.add(command)
            session.flush()

            operation = session.scalar(
                select(Operation).where(Operation.id == operation_id).with_for_update()
            )
            if operation is None:
                raise DocketError(
                    code="operation_not_found",
                    message="The selected Gmail operation does not exist.",
                )
            revision = session.get(ActionRevision, operation.action_revision_id)
            action = session.get(Action, revision.action_id) if revision is not None else None
            queue_item = (
                session.get(QueueItem, action.queue_item_id)
                if action is not None and action.queue_item_id is not None
                else None
            )
            if (
                revision is None
                or action is None
                or queue_item is None
                or operation.operation_type not in GMAIL_MUTATION_ACTION_TYPES
            ):
                raise DocketError(
                    code="invalid_operation_state",
                    message="The operation is not a queue-bound Gmail mutation.",
                )
            latest_attempt = session.scalar(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.operation_id == operation.id)
                .order_by(
                    ExecutionAttempt.attempt_number.desc(),
                    ExecutionAttempt.started_at.desc(),
                )
                .limit(1)
            )
            if (
                operation.status != OperationStatus.FAILED.value
                or operation.last_error_code != "gmail_invalid_response"
                or latest_attempt is None
                or latest_attempt.kind != "execute"
                or latest_attempt.status != "failed"
                or latest_attempt.error_code != "gmail_invalid_response"
                or not str(latest_attempt.provider_request_id or "").startswith("call-started:")
            ):
                raise DocketError(
                    code="gmail_recovery_not_allowed",
                    message=(
                        "Only a failed Gmail call with the known invalid-response "
                        "signature can enter reconciliation."
                    ),
                    details={
                        "status": operation.status,
                        "error_code": operation.last_error_code,
                    },
                )

            now = utc_now()
            operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
            operation.next_attempt_at = now
            operation.lease_token = None
            operation.leased_until = None
            action.status = ActionStatus.RECONCILIATION_REQUIRED.value
            queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
            queue_item.resolved_at = None
            queue_item.resolution_code = None
            queue_item.version += 1
            session.add(
                AuditEvent(
                    event_type="operation.reconciliation_requested",
                    entity_type="operation",
                    entity_id=operation.id,
                    actor_type="operator",
                    actor_id=actor_id,
                    request_id=command.id,
                    data={
                        "action_revision_id": str(revision.id),
                        "prior_error_code": "gmail_invalid_response",
                        "mode": "provider_state_read_only",
                    },
                )
            )
            session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=(
                        f"discord_projection:{queue_item.id}:gmail-recovery:"
                        f"{operation.id}:v{queue_item.version}"
                    ),
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "action_id": str(action.id),
                        "operation_id": str(operation.id),
                        "status": OperationStatus.RECONCILIATION_REQUIRED.value,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
            result = {
                "request_id": str(command.id),
                "operation_id": str(operation.id),
                "queue_item_id": str(queue_item.id),
                "queue_item_version": queue_item.version,
                "status": operation.status,
                "disposition": "reconciliation_requested",
            }
            self._finish(command, result)
            return result

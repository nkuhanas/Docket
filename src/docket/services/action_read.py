from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import Action, ActionRevision, Approval, Operation


class ActionReadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, action_id: uuid.UUID) -> dict[str, Any]:
        action = self.session.get(Action, action_id)
        if action is None:
            raise DocketError(
                code="action_not_found",
                message="The requested action does not exist.",
                details={"action_id": str(action_id)},
            )
        revision = self.session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == action.id,
                ActionRevision.revision == action.current_revision,
            )
        )
        if revision is None:
            raise DocketError(code="invalid_action_state", message="Current revision is missing.")
        approval = self.session.scalar(
            select(Approval).where(Approval.action_revision_id == revision.id)
        )
        operation = self.session.scalar(
            select(Operation).where(Operation.action_revision_id == revision.id)
        )
        return {
            "action_id": str(action.id),
            "action_type": action.action_type,
            "status": action.status,
            "queue_item_id": str(action.queue_item_id) if action.queue_item_id else None,
            "record_id": str(action.record_id) if action.record_id else None,
            "current_revision": action.current_revision,
            "revision": {
                "action_revision_id": str(revision.id),
                "parameters_sha256": revision.parameters_sha256,
                "preview": revision.preview,
                "preview_sha256": revision.preview_sha256,
                "risk_class": revision.risk_class,
                "target_versions": revision.target_versions,
            },
            "approval": (
                {
                    "approval_id": str(approval.id),
                    "status": approval.status,
                    "expires_at": approval.expires_at.isoformat(),
                }
                if approval
                else None
            ),
            "operation": (
                {
                    "operation_id": str(operation.id),
                    "status": operation.status,
                    "attempt_count": operation.attempt_count,
                    "last_error_code": operation.last_error_code,
                }
                if operation
                else None
            ),
        }

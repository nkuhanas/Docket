from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.enums import ActionStatus, ApprovalStatus, CommandStatus, QueueItemStatus
from docket.models import Action, ActionRevision, Approval, AuditEvent, CommandRequest, QueueItem
from docket.schemas.actions import ProposalResult


def find_materially_identical_pending_proposal(
    session: Session,
    *,
    category: str,
    material_fingerprint: str,
    now: datetime,
) -> ProposalResult | None:
    """Return one still-actionable proposal with the same normalized effect.

    Request-key replay handles the same Discord message. This lookup handles a
    second message that expresses the same effect while the first immutable
    proposal is still awaiting a decision.
    """

    row = session.execute(
        select(QueueItem, Action, ActionRevision, Approval)
        .join(Action, Action.queue_item_id == QueueItem.id)
        .join(
            ActionRevision,
            (ActionRevision.action_id == Action.id)
            & (ActionRevision.revision == Action.current_revision),
        )
        .join(Approval, Approval.action_revision_id == ActionRevision.id)
        .where(
            QueueItem.category == category,
            QueueItem.material_fingerprint == material_fingerprint,
            QueueItem.status == QueueItemStatus.AWAITING_APPROVAL.value,
            Action.status == ActionStatus.APPROVAL_PENDING.value,
            Approval.status == ApprovalStatus.PENDING.value,
            Approval.expires_at > now,
        )
        .order_by(QueueItem.created_at.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None

    queue_item, action, revision, approval = row
    proposed = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.event_type == "action.proposed",
            AuditEvent.entity_type == "action",
            AuditEvent.entity_id == action.id,
            AuditEvent.request_id.is_not(None),
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    if proposed is None or proposed.request_id is None:
        return None
    command = session.get(CommandRequest, proposed.request_id)
    if (
        command is None
        or command.status != CommandStatus.SUCCEEDED.value
        or command.result is None
    ):
        return None

    result: dict[str, Any] = dict(command.result)
    expected = {
        "queue_item_id": str(queue_item.id),
        "action_id": str(action.id),
        "action_revision_id": str(revision.id),
        "approval_id": str(approval.id),
    }
    if any(str(result.get(key)) != value for key, value in expected.items()):
        return None
    result["disposition"] = "matched_existing"
    return ProposalResult.model_validate(result)

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import AttentionCase, AuditEvent, CaseItem, ChangeSet, QueueItem
from docket.models.base import utc_now
from docket.schemas.authority import CanonicalChangeInput


class AttentionCaseResolutionService:
    """Resolve only the exact AttentionCases named in authenticated intent."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, Any]:
        return {"attention_case_resolution": self.apply}

    def apply(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: CanonicalChangeInput,
    ) -> list[str]:
        if change.action != "update" or change.object_ref is None:
            raise DocketError(
                code="invalid_attention_case_resolution",
                message="AttentionCase resolution requires an exact case update.",
            )
        case = self.session.scalar(
            select(AttentionCase)
            .where(AttentionCase.ref_id == change.object_ref)
            .with_for_update()
        )
        if case is None:
            raise DocketError(
                code="attention_case_not_found",
                message="AttentionCase public reference was not found.",
            )
        if case.status != "open":
            raise DocketError(
                code="attention_case_not_open",
                message="Only an open AttentionCase can be resolved.",
                details={"status": case.status},
            )
        resolution_status = change.payload.get("resolution_status")
        if resolution_status not in {"resolved", "suppressed", "cancelled"}:
            raise DocketError(
                code="invalid_attention_case_resolution",
                message="AttentionCase resolution status is invalid.",
            )
        raw_dispositions = change.payload.get("item_dispositions")
        if not isinstance(raw_dispositions, dict):
            raise DocketError(
                code="attention_case_item_dispositions_required",
                message="Every open CaseItem requires an explicit resolution disposition.",
            )
        items = list(
            self.session.scalars(
                select(CaseItem)
                .where(CaseItem.attention_case_id == case.id)
                .order_by(CaseItem.created_at, CaseItem.ref_id)
                .with_for_update()
            )
        )
        open_refs = {item.ref_id for item in items if item.status == "open"}
        if set(raw_dispositions) != open_refs or any(
            disposition not in {"resolved", "rejected"}
            for disposition in raw_dispositions.values()
        ):
            raise DocketError(
                code="attention_case_items_unresolved",
                message="Case resolution must explicitly resolve or reject every open CaseItem.",
                details={"open_item_refs": sorted(open_refs)},
            )
        for item in items:
            if item.ref_id in raw_dispositions:
                item.status = str(raw_dispositions[item.ref_id])
                item.version += 1
        now = utc_now()
        case.status = str(resolution_status)
        case.resolved_at = now
        case.version += 1
        decision_refs = [ref for ref in change.basis_refs if ref.startswith("dec_")]
        case.resolution_decision_ref = decision_refs[0] if decision_refs else None
        if case.queue_item_id is not None:
            queue_item = self.session.get(QueueItem, case.queue_item_id)
            if queue_item is not None:
                queue_item.status = "completed"
                queue_item.resolved_at = now
                queue_item.resolution_code = f"attention_case_{resolution_status}"
                queue_item.version += 1
        affected_refs = [case.ref_id, *sorted(open_refs)]
        self.session.add(
            AuditEvent(
                event_type="attention_case.resolved",
                entity_type="attention_case",
                entity_id=case.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=case.ref_id,
                affected_refs=affected_refs,
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "resolution_status": resolution_status,
                    "item_dispositions": dict(raw_dispositions),
                },
            )
        )
        return affected_refs

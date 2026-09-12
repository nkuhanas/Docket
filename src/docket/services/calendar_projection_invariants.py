from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import (
    CanonicalEvent,
    ChangeSet,
    Operation,
    OperationTarget,
    ProviderEventBinding,
)


def missing_event_binding_diagnostic(
    session: Session, *, target_ref: str, target_kind: str,
) -> dict[str, Any]:
    """Explain a missing binding without resubmitting or claiming provider work.

    A committed create may still be queued, uncertain or failed. That is an
    execution dependency, not an unresolved Operator choice. Look up only the
    exact target, and never pick one arbitrarily if its create history is ambiguous.
    """
    creates = list(session.scalars(
        select(Operation).join(OperationTarget).join(
            ChangeSet, ChangeSet.ref_id == Operation.originating_changeset_ref,
        ).where(
            OperationTarget.canonical_target_ref == target_ref,
            OperationTarget.target_kind == target_kind,
            Operation.operation_type == "calendar_create_event",
            ChangeSet.state == "committed",
        ).order_by(Operation.created_at, Operation.ref_id).limit(2)
    ))
    details: dict[str, Any] = {
        "target_ref": target_ref, "category": "provider_readiness",
        "field_path": ["object_ref"], "constraint": "active_provider_event_binding",
        "next_action": "inspect_missing_provider_binding",
        "status_read": {
            "tool": "docket_get_history_entry", "arguments": {"ref": target_ref},
        },
        "recovery_rule": "preserve_draft_do_not_recreate_target_or_request_authorization",
    }
    if len(creates) != 1:
        details["binding_diagnostic"] = (
            "no_committed_create_operation" if not creates else "ambiguous_create_history"
        )
        return details
    operation = creates[0]
    details.update({
        "creation_operation_ref": operation.ref_id,
        "creation_operation_state": operation.status,
        "status_read": {
            "tool": "docket_get_history_entry",
            "arguments": {"ref": operation.originating_changeset_ref, "view": "delivery"},
        },
    })
    details["next_action"] = {
        "pending": "follow_original_provider_delivery",
        "running": "follow_original_provider_delivery",
        "reconciliation_required": "await_original_provider_reconciliation",
        "failed": "recover_original_provider_operation",
        "partial_failed": "recover_original_provider_operation",
        "succeeded": "inspect_confirmed_operation_missing_binding",
    }[operation.status]
    if operation.last_error_code:
        details["creation_error_code"] = operation.last_error_code
    return details


@dataclass(frozen=True)
class CalendarProjectionInvariantViolation:
    event_ref: str
    originating_changeset_ref: str
    lane_ref: str | None
    reason: str = "committed_event_missing_provider_projection"


class CalendarProjectionInvariantService:
    """Detect committed Calendar events that lost their required provider intent."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _created_by(changeset: ChangeSet, event: CanonicalEvent) -> bool:
        for raw_change in changeset.event_changes:
            if raw_change.get("action") != "create":
                continue
            change_id = str(raw_change.get("change_id", ""))
            create_spec = raw_change.get("create_spec")
            if not change_id or not isinstance(create_spec, dict):
                continue
            canonical_key = str(
                create_spec.get("canonical_key")
                or f"changeset:{changeset.ref_id}:{change_id}"
            )
            if canonical_key == event.canonical_key:
                return True
        return False

    def find_violations(
        self, *, limit: int = 25
    ) -> list[CalendarProjectionInvariantViolation]:
        operation_target_refs = {
            ref
            for refs in self.session.scalars(select(Operation.canonical_target_refs))
            for ref in refs
        }
        candidates = list(
            self.session.scalars(
                select(CanonicalEvent)
                .where(
                    CanonicalEvent.status == "active",
                )
                .order_by(CanonicalEvent.created_at, CanonicalEvent.ref_id)
            )
        )
        violations: list[CalendarProjectionInvariantViolation] = []
        for event in candidates:
            if event.ref_id in operation_target_refs:
                continue
            if self.session.scalar(
                select(ProviderEventBinding.id).where(
                    ProviderEventBinding.canonical_target_ref == event.ref_id,
                    ProviderEventBinding.target_kind == "event",
                )
            ) is not None:
                continue
            origin = self.session.scalar(
                select(ChangeSet).where(
                    ChangeSet.ref_id == event.created_by_changeset_ref,
                    ChangeSet.state == "committed",
                )
            )
            if (
                origin is None
                or origin.provider_intents
                or not self._created_by(origin, event)
            ):
                continue
            violations.append(
                CalendarProjectionInvariantViolation(
                    event_ref=event.ref_id,
                    originating_changeset_ref=origin.ref_id,
                    lane_ref=event.lane_ref,
                )
            )
            if len(violations) >= limit:
                break
        return violations

    @staticmethod
    def projection(
        violations: list[CalendarProjectionInvariantViolation], *, limit: int = 25
    ) -> dict[str, Any]:
        return {
            "ok": not violations,
            "count": len(violations),
            "truncated": len(violations) >= limit,
            "items": [
                {
                    "event_ref": item.event_ref,
                    "originating_changeset_ref": item.originating_changeset_ref,
                    "lane_ref": item.lane_ref,
                    "reason": item.reason,
                }
                for item in violations
            ],
        }

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import (
    CalendarLink,
    CanonicalEvent,
    ChangeSet,
    Operation,
    ProviderEventBinding,
)


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
                    CanonicalEvent.provenance_status == "complete",
                    CanonicalEvent.created_by_changeset_ref.is_not(None),
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
                    ProviderEventBinding.canonical_event_id == event.id
                )
            ) is not None or self.session.scalar(
                select(CalendarLink.id).where(CalendarLink.canonical_event_id == event.id)
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

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import CalendarLane, ProviderAccount
from docket.schemas.calendar import CalendarLaneResult


@dataclass(frozen=True, slots=True)
class LaneDefinition:
    display_name: str
    color_hex: str


LANE_DEFINITIONS: Final[dict[str, LaneDefinition]] = {
    "academic": LaneDefinition("Docket · Academic", "#3F51B5"),
    "work": LaneDefinition("Docket · Work", "#D50000"),
    "organizations": LaneDefinition("Docket · Organizations", "#0B8043"),
    "personal": LaneDefinition("Docket · Personal", "#8E24AA"),
    "unsorted": LaneDefinition("Docket", "#F6BF26"),
}


def lane_result(lane: CalendarLane) -> CalendarLaneResult:
    return CalendarLaneResult(
        ref=lane.ref_id,
        lane_id=lane.id,
        lane=lane.lane,
        display_name=lane.display_name,
        color_hex=lane.color_hex,
        status=lane.status,
        account_id=lane.account_id,
        calendar_id=lane.calendar_id,
        operator_policy_text=lane.operator_policy_text,
        metadata_json=lane.metadata_json,
        enabled=lane.enabled,
        priority=lane.priority,
        basis_refs=lane.basis_refs,
        decision_refs=lane.decision_refs,
        source_refs=lane.source_refs,
        created_by_changeset_ref=lane.created_by_changeset_ref,
        provenance_status=lane.provenance_status,
        version=lane.version,
    )


class CalendarLaneService:
    """Canonical CalendarLane lookup and deterministic default initialization."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def ensure_for_account(self, account: ProviderAccount) -> list[CalendarLane]:
        existing = {
            lane.lane: lane
            for lane in self.session.scalars(
                select(CalendarLane).where(CalendarLane.account_id == account.id)
            )
        }
        for lane_name, definition in LANE_DEFINITIONS.items():
            if lane_name in existing:
                continue
            lane = CalendarLane(
                account_id=account.id,
                lane=lane_name,
                display_name=definition.display_name,
                color_hex=definition.color_hex,
                calendar_id=(self.settings.google_calendar_id if lane_name == "unsorted" else None),
                status="active" if lane_name == "unsorted" else "unprovisioned",
            )
            try:
                with self.session.begin_nested():
                    self.session.add(lane)
                    self.session.flush()
            except IntegrityError:
                lane = self.session.scalar(
                    select(CalendarLane).where(
                        CalendarLane.account_id == account.id,
                        CalendarLane.lane == lane_name,
                    )
                )
                if lane is None:
                    raise
            existing[lane_name] = lane
        defaults = [existing[name] for name in LANE_DEFINITIONS]
        custom = sorted(
            (lane for name, lane in existing.items() if name not in LANE_DEFINITIONS),
            key=lambda lane: lane.lane,
        )
        return [*defaults, *custom]

    def list_lanes(self, account_id: uuid.UUID) -> list[CalendarLaneResult]:
        account = self._account(account_id)
        return [lane_result(lane) for lane in self.ensure_for_account(account)]

    def calendar_ids(self, account_id: uuid.UUID) -> list[str]:
        account = self._account(account_id)
        return [
            lane.calendar_id
            for lane in self.ensure_for_account(account)
            if lane.status == "active" and lane.calendar_id is not None
        ]

    def require_active(
        self,
        account_id: uuid.UUID,
        *,
        lane_name: str | None = None,
        calendar_id: str | None = None,
    ) -> CalendarLane:
        account = self._account(account_id)
        matches = [
            lane
            for lane in self.ensure_for_account(account)
            if (lane_name is None or lane.lane == lane_name)
            and (calendar_id is None or lane.calendar_id == calendar_id)
        ]
        if len(matches) != 1 or matches[0].status != "active" or not matches[0].calendar_id:
            raise DocketError(
                code="calendar_lane_unavailable",
                message="The selected Calendar lane is not provisioned and active.",
                details={"lane": lane_name, "calendar_id": calendar_id},
            )
        return matches[0]

    def _account(self, account_id: uuid.UUID) -> ProviderAccount:
        account = self.session.get(ProviderAccount, account_id)
        if account is None or account.provider != "google" or not account.enabled:
            raise DocketError(
                code="invalid_account",
                message="Calendar lanes require an enabled Google Calendar account.",
                details={"account_id": str(account_id)},
            )
        return account

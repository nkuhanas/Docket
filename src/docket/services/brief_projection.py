from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import (
    Action,
    ActionRevision,
    Approval,
    DailyBrief,
    DailyBriefItem,
    DiscordDailyThread,
    DiscordProjection,
    SemanticCandidate,
)


@dataclass(frozen=True)
class ProjectionRefreshTarget:
    queue_item_id: uuid.UUID
    projection_id: uuid.UUID | None = None
    target_local_date: str | None = None

    def payload(self) -> dict[str, str]:
        result = {"queue_item_id": str(self.queue_item_id)}
        if self.projection_id is not None:
            result["projection_id"] = str(self.projection_id)
        if self.target_local_date is not None:
            result["target_local_date"] = self.target_local_date
        return result


def morning_brief_contains_queue_item(
    session: Session,
    *,
    brief_queue_item_id: uuid.UUID,
    child_queue_item_id: uuid.UUID,
) -> bool:
    return (
        session.scalar(
            select(DailyBriefItem.brief_id)
            .join(DailyBrief, DailyBrief.id == DailyBriefItem.brief_id)
            .join(
                SemanticCandidate,
                SemanticCandidate.id == DailyBriefItem.semantic_candidate_id,
            )
            .where(
                DailyBrief.queue_item_id == brief_queue_item_id,
                DailyBrief.brief_kind == "morning",
                SemanticCandidate.queue_item_id == child_queue_item_id,
            )
            .limit(1)
        )
        is not None
    )


def morning_brief_selects_queue_item(
    session: Session,
    *,
    projection: DiscordProjection,
    child_queue_item_id: uuid.UUID,
) -> bool:
    if projection.view_mode != "brief_review" or projection.view_action_revision_id is None:
        return False
    selected_queue_item_id = session.scalar(
        select(Action.queue_item_id)
        .join(ActionRevision, ActionRevision.action_id == Action.id)
        .where(ActionRevision.id == projection.view_action_revision_id)
    )
    return selected_queue_item_id == child_queue_item_id and morning_brief_contains_queue_item(
        session,
        brief_queue_item_id=projection.queue_item_id,
        child_queue_item_id=child_queue_item_id,
    )


def projection_refresh_target(
    session: Session,
    *,
    child_queue_item_id: uuid.UUID,
    projection: DiscordProjection | None,
) -> ProjectionRefreshTarget:
    if projection is None:
        return ProjectionRefreshTarget(queue_item_id=child_queue_item_id)
    if projection.queue_item_id != child_queue_item_id and not morning_brief_contains_queue_item(
        session,
        brief_queue_item_id=projection.queue_item_id,
        child_queue_item_id=child_queue_item_id,
    ):
        raise DocketError(
            code="invalid_brief_projection",
            message="The projection does not contain this morning-brief decision.",
        )
    daily_thread = session.get(DiscordDailyThread, projection.daily_thread_id)
    if daily_thread is None:
        raise DocketError(
            code="invalid_brief_projection",
            message="The projection's daily thread is unavailable.",
        )
    return ProjectionRefreshTarget(
        queue_item_id=projection.queue_item_id,
        projection_id=projection.id,
        target_local_date=daily_thread.local_date.isoformat(),
    )


def operation_projection_target(
    session: Session,
    *,
    child_queue_item_id: uuid.UUID,
    approval_id: uuid.UUID | None,
) -> ProjectionRefreshTarget:
    approval = session.get(Approval, approval_id) if approval_id is not None else None
    projection = (
        session.get(DiscordProjection, approval.response_projection_id)
        if approval is not None and approval.response_projection_id is not None
        else None
    )
    return projection_refresh_target(
        session,
        child_queue_item_id=child_queue_item_id,
        projection=projection,
    )

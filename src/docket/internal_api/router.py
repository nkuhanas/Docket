from fastapi import APIRouter, Depends, HTTPException, status

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.errors import DocketError
from docket.internal_api.auth import require_hermes_service
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse
from docket.models.base import utc_now
from docket.providers.google.runtime import get_calendar_read_provider
from docket.services.approvals import ApprovalService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.local_actions import LocalActionService
from docket.services.proposal_controls import ProposalControlService

router = APIRouter(
    prefix="/internal/v1/discord",
    tags=["trusted-internal"],
    dependencies=[Depends(require_hermes_service)],
)


@router.post("/approval-responses")
def approval_response(payload: ApprovalResponse) -> dict[str, object]:
    failure: DocketError | None = None
    result: dict[str, object] | None = None
    with session_scope() as session:
        try:
            result = ApprovalService(session).respond(payload)
        except DocketError as exc:
            failure = exc
    if failure is not None:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if failure.code == "approval_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=error_status, detail=failure.as_dict()["error"])
    assert result is not None
    return result


@router.post("/local-action-responses")
def local_action_response(payload: LocalActionResponse) -> dict[str, object]:
    failure: DocketError | None = None
    result: dict[str, object] | None = None
    try:
        refresh_started_at = None
        if payload.transition == "proposal_refresh":
            settings = get_settings()
            if not settings.calendar_reads_enabled:
                raise DocketError(
                    code="proposal_refresh_unavailable",
                    message="Calendar reads are disabled, so this proposal cannot refresh.",
                )
            with session_scope() as session:
                account_id, calendar_id = ProposalControlService(
                    session
                ).prepare_refresh(payload)
            refresh_started_at = utc_now()
            CalendarSyncService(
                get_session_factory(),
                get_calendar_read_provider(),
                settings,
            ).require_fresh(account_id, calendar_id)
        with session_scope() as session:
            result = (
                LocalActionService(session).respond(payload)
                if payload.transition == "local_action"
                else ProposalControlService(session).respond(
                    payload,
                    refresh_started_at=refresh_started_at,
                )
            )
    except DocketError as exc:
        failure = exc
    if failure is not None:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if failure.code in {"queue_item_not_found", "local_action_not_found"}
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=error_status, detail=failure.as_dict()["error"])
    assert result is not None
    return result

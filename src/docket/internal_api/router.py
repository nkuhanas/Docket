from fastapi import APIRouter, Depends, HTTPException, status

from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.internal_api.auth import require_hermes_service
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse
from docket.services.approvals import ApprovalService

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
def local_action_response(_payload: LocalActionResponse) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Queue actions begin in Milestone 3; authentication boundary is active.",
    )

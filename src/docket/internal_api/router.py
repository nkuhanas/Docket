from fastapi import APIRouter, Depends, HTTPException, status

from docket.internal_api.auth import require_hermes_service
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse

router = APIRouter(
    prefix="/internal/v1/discord",
    tags=["trusted-internal"],
    dependencies=[Depends(require_hermes_service)],
)


@router.post("/approval-responses")
def approval_response(_payload: ApprovalResponse) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Approval persistence begins in Milestone 2; authentication boundary is active.",
    )


@router.post("/local-action-responses")
def local_action_response(_payload: LocalActionResponse) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Queue actions begin in Milestone 3; authentication boundary is active.",
    )

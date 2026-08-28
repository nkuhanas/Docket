import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from starlette.requests import Request

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.errors import DocketError
from docket.internal_api.auth import require_hermes_service
from docket.internal_api.schemas import (
    AgentResponseCapture,
    AgentResponseDeliveryUpdate,
    AgentTurnNoResponse,
    ApprovalResponse,
    LocalActionResponse,
    McpTraceUpdate,
    OperatorUtteranceCapture,
    SpecificationSignoffCapture,
)
from docket.models.base import utc_now
from docket.providers.google.runtime import get_calendar_read_provider
from docket.services.approvals import ApprovalService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.local_actions import LocalActionService
from docket.services.mcp_traces import McpTraceService
from docket.services.proposal_controls import ProposalControlService
from docket.services.provenance import ProvenanceService

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/discord",
    tags=["trusted-internal"],
    dependencies=[Depends(require_hermes_service)],
)


def _wake_projection_worker(request: Request) -> None:
    wake = getattr(request.app.state, "wake_discord_projection", None)
    if not callable(wake):
        return
    try:
        wake()
    except Exception:
        # The committed outbox remains authoritative; polling is the fallback.
        logger.exception("discord_projection_wake_failed")


@router.post("/approval-responses")
def approval_response(request: Request, payload: ApprovalResponse) -> dict[str, object]:
    failure: DocketError | None = None
    result: dict[str, object] | None = None
    with session_scope() as session:
        try:
            result = ApprovalService(session).respond(payload)
        except DocketError as exc:
            failure = exc
    if failure is not None:
        if failure.code == "target_version_changed":
            _wake_projection_worker(request)
        error_status = (
            status.HTTP_404_NOT_FOUND
            if failure.code == "approval_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=error_status, detail=failure.as_dict()["error"])
    assert result is not None
    _wake_projection_worker(request)
    return result


@router.post("/local-action-responses")
def local_action_response(request: Request, payload: LocalActionResponse) -> dict[str, object]:
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
                account_id, calendar_id = ProposalControlService(session).prepare_refresh(payload)
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
    _wake_projection_worker(request)
    return result


@router.put("/mcp-traces/{trace_id}")
def mcp_trace_update(
    request: Request,
    trace_id: str,
    payload: McpTraceUpdate,
) -> dict[str, object]:
    try:
        parsed_trace_id = uuid.UUID(trace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_mcp_trace_id", "message": "trace_id must be a UUID"},
        ) from exc
    failure: DocketError | None = None
    result: dict[str, object] | None = None
    with session_scope() as session:
        try:
            result = McpTraceService(session).update(parsed_trace_id, payload)
        except DocketError as exc:
            failure = exc
    if failure is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=failure.as_dict()["error"],
        )
    assert result is not None
    _wake_projection_worker(request)
    return result


@router.post("/operator-utterances")
def operator_utterance_capture(payload: OperatorUtteranceCapture) -> dict[str, object]:
    try:
        with session_scope() as session:
            return ProvenanceService(session).capture_operator_utterance(payload)
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


@router.post("/specification-signoffs")
def specification_signoff_capture(payload: SpecificationSignoffCapture) -> dict[str, object]:
    try:
        with session_scope() as session:
            return ProvenanceService(session).record_final_architecture_signoff(payload)
    except DocketError as exc:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "operator_utterance_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=error_status,
            detail=exc.as_dict()["error"],
        ) from exc


@router.post("/agent-responses")
def agent_response_capture(payload: AgentResponseCapture) -> dict[str, object]:
    try:
        with session_scope() as session:
            return ProvenanceService(session).capture_agent_response(payload)
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


@router.post("/agent-turns/no-response")
def agent_turn_no_response(payload: AgentTurnNoResponse) -> dict[str, object]:
    try:
        with session_scope() as session:
            return ProvenanceService(session).finalize_agent_turn_without_response(payload)
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


@router.put("/agent-responses/{response_ref}/delivery")
def agent_response_delivery_update(
    response_ref: str,
    payload: AgentResponseDeliveryUpdate,
) -> dict[str, object]:
    if response_ref != payload.response_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "agent_response_ref_mismatch",
                "message": "Path and body response references differ.",
            },
        )
    try:
        with session_scope() as session:
            return ProvenanceService(session).update_agent_response_delivery(payload)
    except DocketError as exc:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "agent_response_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=error_status,
            detail=exc.as_dict()["error"],
        ) from exc

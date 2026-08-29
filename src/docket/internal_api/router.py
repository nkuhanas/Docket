import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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
    GatewayLifetimeHeartbeat,
    GatewayLifetimeRegister,
    GatewayLifetimeShutdown,
    LocalActionResponse,
    McpTraceUpdate,
    OperatorUtteranceCapture,
    SemanticOptionSelection,
    SpecificationSignoffCapture,
)
from docket.models import DeferredIngress, SemanticRequest
from docket.models.base import utc_now
from docket.providers.google.runtime import get_calendar_read_provider
from docket.schemas.authority import ChangeSetContent
from docket.services.approvals import ApprovalService
from docket.services.calendar_sync import CalendarSyncService
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.local_actions import LocalActionService
from docket.services.mcp_traces import McpTraceService
from docket.services.proposal_controls import ProposalControlService
from docket.services.provenance import ProvenanceService
from docket.services.semantic_options import SemanticOptionService

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/discord",
    tags=["trusted-internal"],
    dependencies=[Depends(require_hermes_service)],
)


@router.post("/gateway-lifetimes")
def gateway_lifetime_register(payload: GatewayLifetimeRegister) -> dict[str, object]:
    try:
        with session_scope() as session:
            return GatewayLifetimeService(session).register(
                registration_key=payload.registration_key,
                instance_kind=payload.instance_kind,
            )
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


@router.put("/gateway-lifetimes/{gateway_instance_ref}/heartbeat")
def gateway_lifetime_heartbeat(
    gateway_instance_ref: str,
    payload: GatewayLifetimeHeartbeat,
) -> dict[str, object]:
    if gateway_instance_ref != payload.gateway_instance_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway_lifetime_binding_mismatch",
                "message": "Gateway lifetime path and body differ.",
            },
        )
    try:
        with session_scope() as session:
            return GatewayLifetimeService(session).heartbeat(
                gateway_instance_ref,
                status=payload.status,
            )
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


@router.put("/gateway-lifetimes/{gateway_instance_ref}/shutdown")
def gateway_lifetime_shutdown(
    gateway_instance_ref: str,
    payload: GatewayLifetimeShutdown,
) -> dict[str, object]:
    if gateway_instance_ref != payload.gateway_instance_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway_lifetime_binding_mismatch",
                "message": "Gateway lifetime path and body differ.",
            },
        )
    try:
        with session_scope() as session:
            return GatewayLifetimeService(session).clean_shutdown(gateway_instance_ref)
    except DocketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.as_dict()["error"],
        ) from exc


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


@router.post("/semantic-option-selections")
def semantic_option_selection(payload: SemanticOptionSelection) -> dict[str, object]:
    try:
        with session_scope() as session:
            selection = SemanticOptionService(session).capture_selection(payload)
    except DocketError as exc:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if exc.code in {"semantic_option_not_found", "intent_session_not_found"}
            else status.HTTP_403_FORBIDDEN
            if exc.code == "unauthorized_interaction"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=error_status,
            detail=exc.as_dict()["error"],
        ) from exc
    execution: dict[str, object]
    try:
        compiled = ChangeSetContent.model_validate(selection["compiled_content"])
        with session_scope() as session:
            semantic_request = session.scalar(
                select(SemanticRequest).where(
                    SemanticRequest.ref_id == selection["semantic_request_ref"]
                )
            )
            if (
                semantic_request is not None
                and semantic_request.authority_availability == "consumed_committed"
                and semantic_request.committed_changeset_ref is not None
            ):
                execution = {
                    "ok": True,
                    "ref": semantic_request.committed_changeset_ref,
                    "state": "committed",
                    "summary": "Replayed the existing selected semantic result.",
                    "affected_refs": [
                        semantic_request.ref_id,
                        semantic_request.committed_changeset_ref,
                    ],
                    "basis_refs": list(semantic_request.origin_utterance_refs),
                    "next": None,
                    "warnings": [],
                    "disposition": "replayed_request",
                }
            else:
                execution = InteractiveAuthorityService(session).process_turn(
                    utterance_ref=str(selection["utterance_ref"]),
                    request_key=str(selection["request_key"]),
                    actor_id=payload.discord_user_id,
                    intent_session_ref=str(selection["intent_session_ref"]),
                    expected_session_version=None,
                    statements=[],
                    relations=[],
                    resolved_intent_json={
                        "selected_option": selection["visible_choice_text"],
                        "authority_scope_hash": selection["authority_scope_hash"],
                    },
                    blocking_clarifications=[],
                    content=compiled,
                    changeset_ref=None,
                    expected_changeset_version=None,
                    semantic_request_ref=str(selection["semantic_request_ref"]),
                    authority_scope_hash=str(selection["authority_scope_hash"]),
                    precondition_hash=str(selection["precondition_hash"]),
                    gateway_instance_ref=payload.gateway_instance_ref,
                )
            ingress = session.scalar(
                select(DeferredIngress).where(
                    DeferredIngress.ref_id == selection["deferred_ingress_ref"]
                )
            )
            if ingress is not None:
                ingress.status = "completed"
                ingress.completed_at = utc_now()
    except DocketError as exc:
        execution = {
            "ok": False,
            "state": "blocked_validation",
            "summary": "Decision remains authorized, but Docket could not execute it.",
            "disposition": "rejected_validation",
            "error": exc.as_dict()["error"],
        }
    disposition = str(execution.get("disposition", "unknown"))
    if execution.get("state") == "committed" or disposition == "replayed_request":
        response_text = f"Done — {selection['visible_choice_text']}"
    else:
        error = execution.get("error")
        error_code = (
            str(error.get("code", "unknown"))
            if isinstance(error, dict)
            else disposition
        )
        response_text = (
            "I recorded your decision, but Docket could not apply it "
            f"(`{error_code}`). Your authorization remains available; you do not "
            "need to repeat it."
        )
    trace_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"docket:semantic-option-selection:{payload.discord_interaction_id}",
    )
    with session_scope() as session:
        response = ProvenanceService(session).capture_agent_response(
            AgentResponseCapture(
                request_id=payload.request_id,
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
                parent_channel_id=payload.parent_channel_id,
                source_message_id=payload.discord_interaction_id,
                actor_id=payload.discord_user_id,
                utterance_ref=str(selection["utterance_ref"]),
                turn_id=f"semantic-option:{payload.discord_interaction_id}",
                session_id=str(selection["semantic_request_ref"]),
                model_identifier="docket-deterministic-selection-v1",
                verbatim_text=response_text,
                generated_at=utc_now(),
                trace_id=trace_id,
                gateway_instance_ref=payload.gateway_instance_ref,
            )
        )
    selection_affected = selection.get("affected_refs")
    execution_affected = execution.get("affected_refs")
    return {
        **selection,
        "state": execution.get("state", selection["state"]),
        "summary": execution.get("summary", selection["summary"]),
        "affected_refs": list(
            dict.fromkeys(
                [
                    *(selection_affected if isinstance(selection_affected, list) else []),
                    *(execution_affected if isinstance(execution_affected, list) else []),
                    str(response["ref"]),
                ]
            )
        ),
        "disposition": disposition,
        "execution": execution,
        "response_ref": response["ref"],
        "response_text": response_text,
        "response_delivery_state": response["state"],
    }


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

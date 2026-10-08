"""Transport-neutral request admission and optional conversation capture."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.internal_api.auth import require_hermes_service
from docket.internal_api.schemas import AttachmentManifest
from docket.models import ExecutionLease
from docket.schemas.common import StrictModel
from docket.services.agent_requests import AgentRequestService
from docket.services.attachment_evidence import AttachmentCapture, AttachmentEvidenceService
from docket.services.continuity import ContinuityService


async def require_interactive_service(
    _authenticated: None = Depends(require_hermes_service),
) -> AsyncIterator[None]:
    settings = get_settings()
    for path in (settings.docket_triage_token_file, settings.docket_read_only_token_file):
        if (
            path is not None
            and path.is_file()
            and (settings.read_secret(path) == settings.hermes_to_docket_token())
        ):
            raise HTTPException(status_code=401, detail="Workload credentials must be distinct")
    principal = AgentPrincipal(
        principal_ref="agent:interactive",
        operator_ref=f"operator:{settings.operator_discord_user_id}",
        role="interactive",
        permissions=frozenset({"read", "stage", "commit", "resolve_conflict"}),
        enabled=settings.interactive_agent_enabled,
        expires_at=settings.interactive_agent_expires_at,
    )
    try:
        principal.require("stage")
    except DocketError as exc:
        raise HTTPException(status_code=403, detail=exc.as_dict()["error"]) from exc
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        yield
    finally:
        agent_call_context.reset(token)


router = APIRouter(
    prefix="/internal/v1/agent",
    tags=["trusted-agent"],
    dependencies=[Depends(require_interactive_service)],
)


class RequestAdmission(StrictModel):
    request_key: str = Field(min_length=1, max_length=512)
    conversation_ref: str | None = Field(default=None, min_length=1, max_length=512)
    execution_key: str | None = Field(default=None, min_length=1, max_length=255)


class RecordCapture(StrictModel):
    request_ref: str = Field(pattern=r"^req_[0-9A-HJKMNP-TV-Z]{26}$")
    record_key: str = Field(min_length=1, max_length=255)
    record_kind: str = Field(min_length=1, max_length=32)
    text: str | None = Field(default=None, max_length=65536)
    gap_code: str | None = Field(default=None, min_length=1, max_length=128)


class AttachmentRecord(StrictModel):
    request_ref: str = Field(pattern=r"^req_[0-9A-HJKMNP-TV-Z]{26}$")
    attachments: list[AttachmentManifest] = Field(max_length=10)


class ExecutionCompletion(StrictModel):
    request_ref: str = Field(pattern=r"^req_[0-9A-HJKMNP-TV-Z]{26}$")
    execution_key: str = Field(min_length=1, max_length=255)
    completion_token: str = Field(pattern=r"^[0-9a-f]{32}$")


@router.post("/requests")
def admit_request(body: RequestAdmission) -> dict[str, object]:
    try:
        with session_scope() as session:
            root = AgentRequestService(session).admit(
                request_key=body.request_key,
                conversation_ref=body.conversation_ref,
            )
            result: dict[str, object] = {
                "ok": True,
                "request_ref": root.ref_id,
                "state": root.state,
            }
            if body.execution_key is not None and root.state == "active":
                result.update(
                    AgentRequestService(session).claim_foreground_execution(
                        root.ref_id,
                        execution_key=body.execution_key,
                    )
                )
            return result
    except DocketError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()["error"]) from exc


@router.post("/executions/complete")
def complete_execution(body: ExecutionCompletion) -> dict[str, object]:
    with session_scope() as session:
        AgentRequestService(session).require(
            body.request_ref, permission="stage", allow_committed=True, allow_cancelled=True
        )
        lease = session.scalar(
            select(ExecutionLease).where(
                ExecutionLease.lease_key == f"agent:{body.request_ref}:{body.execution_key}",
                ExecutionLease.completion_token == body.completion_token,
                ExecutionLease.subject_ref == body.request_ref,
            )
        )
        if lease is None:
            raise HTTPException(status_code=422, detail={"code": "execution_binding_mismatch"})
        ContinuityService(session).complete_execution_lease(body.completion_token)
        return {"ok": True}


@router.post("/records")
def capture_record(body: RecordCapture) -> dict[str, object]:
    try:
        with session_scope() as session:
            return AgentRequestService(session).record(**body.model_dump())
    except DocketError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()["error"]) from exc


@router.post("/requests/{request_ref}/cancel")
def cancel_request(request_ref: str) -> dict[str, object]:
    try:
        with session_scope() as session:
            return AgentRequestService(session).cancel(request_ref)
    except DocketError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()["error"]) from exc


@router.post("/attachments")
def capture_attachments(body: AttachmentRecord) -> dict[str, object]:
    try:
        with session_scope() as session:
            root = AgentRequestService(session).require(
                body.request_ref,
                permission="stage",
                allow_committed=True,
            )
            settings = get_settings()
            encoded_lengths = [len(row.plaintext_base64 or "") for row in body.attachments]
            if (
                any(
                    size > 4 * ((settings.attachment_max_bytes + 2) // 3)
                    for size in encoded_lengths
                )
                or sum(encoded_lengths) > 4 * ((settings.attachment_total_max_bytes + 2) // 3) + 40
            ):
                raise DocketError(
                    code="attachment_capture_too_large",
                    message="Attachment capture exceeds limits.",
                )
            captures = [
                AttachmentCapture(
                    transport_attachment_ref=row.transport_attachment_ref,
                    received_at=row.received_at,
                    filename=row.filename,
                    media_type=row.media_type,
                    byte_size=row.byte_size,
                    ingest_error_code=row.ingest_error_code,
                    plaintext=(
                        base64.b64decode(row.plaintext_base64, validate=True)
                        if row.plaintext_base64
                        else None
                    ),
                )
                for row in body.attachments
            ]
            refs = AttachmentEvidenceService(
                session,
                encryption_key=settings.attachment_encryption_key(),
                encryption_key_ref=settings.attachment_encryption_key_ref,
                max_attachment_bytes=settings.attachment_max_bytes,
                max_total_bytes=settings.attachment_total_max_bytes,
            ).plan_request(root, captures)
            return {"ok": True, "source_refs": refs, "capture_method": "agent_reported"}
    except (DocketError, ValueError) as exc:
        code = exc.code if isinstance(exc, DocketError) else "invalid_attachment_capture"
        raise HTTPException(status_code=422, detail={"code": code}) from exc

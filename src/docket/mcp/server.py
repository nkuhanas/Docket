import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from docket.config import get_settings
from docket.database import session_scope
from docket.domain.enums import RecordStatus
from docket.domain.errors import DocketError
from docket.schemas.actions import (
    CalendarActionType,
    CalendarMeetingActionParameters,
    ProposalResult,
    ProposeActionInput,
)
from docket.schemas.records import (
    ArchiveRecordInput,
    CourseData,
    CourseIdentity,
    DiscordId,
    DiscordRequestKey,
    GenericIdentity,
    GenericRecordData,
    RecordSourceInput,
    RecordType,
    StoreRecordInput,
    TermData,
    TermIdentity,
    UpdateRecordInput,
)
from docket.services.accounts import AccountService
from docket.services.actions import ActionService
from docket.services.records import RecordService, serialize_record

mcp = FastMCP(
    "docket",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["docket:8000", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    ),
)


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DocketError):
        return exc.as_dict()
    return {
        "ok": False,
        "error": {"code": "validation_error", "message": str(exc), "details": {}},
    }


def _model_proposal_result(result: ProposalResult) -> dict[str, Any]:
    """Return the proposal result exposed to the model-facing MCP client.

    The short code remains in Docket's durable action/outbox state for an
    operator-only break-glass path. It is deliberately absent from the model
    response so ordinary agent guidance cannot regress from persistent Discord
    controls to legacy typed approval messages.
    """
    payload = result.model_dump(mode="json", exclude={"short_code"})
    payload["approval_surface"] = {
        "kind": "discord_button_card",
        "location": "today's ISO-dated thread under the configured Docket queue",
        "delivery_status": result.projection_status,
        "operator_instruction": (
            "Use the Approve or Reject button on the projected Docket card."
        ),
        "typed_code_policy": "break_glass_only_not_for_agent_guidance",
    }
    return payload


@mcp.tool()
def docket_store_record(
    record_type: RecordType,
    canonical_identity: TermIdentity | CourseIdentity | GenericIdentity,
    title: str,
    data: TermData | CourseData | GenericRecordData,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Store an explicit source-backed assertion in Docket, not Hermes memory.

    Always call this for a current trusted Discord store/save/remember request, even when
    search found the canonical record. Materially equal existing records return
    ``matched_existing`` while attaching the current source provenance. Different data
    returns ``record_conflict`` without attaching provenance. Never copy the existing
    record into a retry to manufacture a match; use ``docket_update_record`` only for an
    explicitly authorized replacement. Search/get calls alone never persist provenance.
    """
    try:
        request = StoreRecordInput(
            record_type=record_type,
            canonical_identity=canonical_identity,
            title=title,
            data=data,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).store(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_record(record_id: str) -> dict[str, Any]:
    """Read exact canonical Docket state by UUID; this read-only tool persists no source."""
    try:
        with session_scope() as session:
            record = RecordService(session).get(uuid.UUID(record_id))
            return {"ok": True, "record": serialize_record(record)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_search_records(
    record_type: RecordType | None = None,
    query: str | None = None,
    status: str = "active",
    limit: int = 20,
) -> dict[str, Any]:
    """Search exact canonical Docket records before answering operational facts.

    This tool is read-only. Never claim a store/save/remember request succeeded from
    search results alone; call ``docket_store_record`` with the current trusted source.
    """
    try:
        record_status = RecordStatus(status)
        with session_scope() as session:
            records = RecordService(session).search(
                record_type=record_type,
                query=query,
                status=record_status,
                limit=limit,
            )
            return {"ok": True, "records": [serialize_record(record) for record in records]}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_update_record(
    record_id: str,
    expected_version: int,
    data: dict[str, Any],
    request_key: str,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Replace validated record data using optimistic locking and idempotency."""
    try:
        request = UpdateRecordInput(
            record_id=uuid.UUID(record_id),
            expected_version=expected_version,
            data=data,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).update(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_archive_record(
    record_id: str,
    expected_version: int,
    request_key: str,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Soft-archive a canonical record; physical deletion is not exposed."""
    try:
        request = ArchiveRecordInput(
            record_id=uuid.UUID(record_id),
            expected_version=expected_version,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).archive(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_accounts() -> dict[str, Any]:
    """List enabled Docket-owned Google accounts and capabilities for explicit selection."""
    try:
        settings = get_settings()
        with session_scope() as session:
            accounts = AccountService(session).list_enabled_google()
            return {
                "ok": True,
                "accounts": [
                    {
                        "account_id": str(account.id),
                        "provider": account.provider,
                        "external_account_id": account.external_account_id,
                        "display_name": account.display_name,
                        "email_address": account.email_address,
                        "capabilities": account.capabilities,
                        "calendar_ids": [settings.google_calendar_id],
                    }
                    for account in accounts
                ],
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_propose_action(
    action_type: CalendarActionType,
    record_id: str,
    expected_record_version: int,
    account_id: str,
    parameters: CalendarMeetingActionParameters,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Propose a typed Calendar write from trusted Discord context.

    Docket derives risk, exact executable parameters, immutable preview, hashes,
    target versions, and approval expiry. Normal approval occurs only through the
    persistent Approve/Reject buttons on the projected card in today's ISO-dated
    Docket queue thread. Do not instruct the operator to type an approval code; that
    compatibility path is break-glass only and is not returned to the model. This tool
    never records or consumes an approval and never contacts Google Calendar.
    """
    try:
        request = ProposeActionInput(
            action_type=action_type,
            record_id=uuid.UUID(record_id),
            expected_record_version=expected_record_version,
            account_id=uuid.UUID(account_id),
            parameters=parameters,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = ActionService(session).propose(request)
            return {"ok": True, **_model_proposal_result(result)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_action(action_id: str) -> dict[str, Any]:
    """Read a redacted action, immutable preview, approval, and operation status."""
    try:
        with session_scope() as session:
            return {"ok": True, "action": ActionService(session).get(uuid.UUID(action_id))}
    except Exception as exc:
        return _error(exc)

import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from docket.database import session_scope
from docket.domain.enums import RecordStatus
from docket.domain.errors import DocketError
from docket.schemas.records import (
    ArchiveRecordInput,
    DiscordId,
    DiscordRequestKey,
    GenericIdentity,
    GenericRecordData,
    RecordSourceInput,
    RecordType,
    RememberRecordInput,
    TermData,
    TermIdentity,
    UpdateRecordInput,
)
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


@mcp.tool()
def docket_remember_record(
    record_type: RecordType,
    canonical_identity: TermIdentity | GenericIdentity,
    title: str,
    data: TermData | GenericRecordData,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Persist an explicit remember/store request in Docket, not Hermes memory.

    Always call this for the current trusted Discord source even when search found the
    canonical record. Existing records return ``matched_existing`` while attaching the
    current source provenance; search/get calls alone do not persist that provenance.
    """
    try:
        request = RememberRecordInput(
            record_type=record_type,
            canonical_identity=canonical_identity,
            title=title,
            data=data,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).remember(request)
            return {"ok": True, **result.model_dump(mode="json")}
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

    This tool is read-only. Never claim a remember/store request succeeded from search
    results alone; call ``docket_remember_record`` with the current trusted source.
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
            return {"ok": True, **result.model_dump(mode="json")}
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
            return {"ok": True, **result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)

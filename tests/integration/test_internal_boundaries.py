import asyncio

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select
from starlette.requests import Request

from docket.config import get_settings
from docket.internal_api.auth import require_hermes_service
from docket.main import health_ready, protect_mcp
from docket.mcp import mcp, triage_mcp
from docket.models import RuntimeLogEntry, ToolInvocation


def _request(path: str, authorization: str | None = None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "server": ("docket", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.integration
def test_internal_api_and_mcp_require_distinct_tokens(session_factory) -> None:
    settings = get_settings()
    with pytest.raises(HTTPException) as missing:
        require_hermes_service(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as wrong_service:
        require_hermes_service(f"Bearer {settings.docket_to_hermes_token()}")
    assert wrong_service.value.status_code == 401
    assert require_hermes_service(f"Bearer {settings.hermes_to_docket_token()}") is None

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    unauthorized = asyncio.run(protect_mcp(_request("/mcp"), call_next))
    assert unauthorized.status_code == 401
    wrong_mcp_service = asyncio.run(
        protect_mcp(
            _request("/mcp", f"Bearer {settings.hermes_to_docket_token()}"),
            call_next,
        )
    )
    assert wrong_mcp_service.status_code == 401
    with session_factory() as session:
        runtime_logs = list(
            session.scalars(
                select(RuntimeLogEntry).order_by(RuntimeLogEntry.occurred_at)
            )
        )
        assert len(runtime_logs) == 2
        assert all(entry.ref_id.startswith("log_") for entry in runtime_logs)
        assert all(entry.event_code == "mcp.authentication_rejected" for entry in runtime_logs)
        assert all(entry.related_refs == [] for entry in runtime_logs)
        assert session.scalar(select(func.count()).select_from(ToolInvocation)) == 0
    authorized = asyncio.run(
        protect_mcp(
            _request("/mcp", f"Bearer {settings.docket_to_hermes_token()}"),
            call_next,
        )
    )
    assert authorized.status_code == 204

    allowed_hosts = ["docket:8000", "127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert mcp.settings.transport_security.allowed_hosts == allowed_hosts
    assert triage_mcp.settings.transport_security.allowed_hosts == allowed_hosts

    response = Response()
    ready = health_ready(response)
    assert response.status_code == 200
    assert ready["calendar_reads_enabled"] is False
    assert ready["external_writes_enabled"] is False
    assert ready["google_oauth"] == "dummy"
    assert ready["encrypted_backup"] == {
        "enabled": False,
        "status": "disabled",
        "local_date": None,
        "completed_at": None,
        "error_code": None,
        "degraded": False,
    }

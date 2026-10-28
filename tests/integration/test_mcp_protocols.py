from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from docket.mcp.instrumented import ProvenanceMCP
from docket.models import ToolInvocation


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.asyncio
async def test_both_protocols_preserve_bounded_results_and_rejection_audit(session_factory, mode):
    server = ProvenanceMCP("protocol-fixture", caller_profile="interactive")

    @server.tool()
    def docket_search_history(limit: int = 25) -> dict[str, object]:
        return {"ok": True, "results": [{"index": i} for i in range(min(limit, 100))]}

    app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan():
        async with server.session_manager.run():
            yield

    async with (
        lifespan(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http,
        Client(
            streamable_http_client("http://127.0.0.1:8000/", http_client=http), mode=mode,
        ) as client,
    ):
        assert client.protocol_version == ("2025-11-25" if mode == "legacy" else mode)
        assert len((await client.list_tools()).tools) == 1
        result = await client.call_tool("docket_search_history", {})
        assert len(result.structured_content["items"]) == 25
        capped = await client.call_tool("docket_search_history", {"limit": 1000})
        assert len(capped.structured_content["items"]) == 100
        rejected = await client.call_tool("docket_search_history", {"limit": "invalid"})
        assert rejected.structured_content["error"]["code"] == "validation_error"
    with session_factory() as session:
        rows = session.scalars(select(ToolInvocation).order_by(ToolInvocation.started_at)).all()
        assert len(rows) == 3
        assert rows[0].transport_state == "completed"
        assert rows[2].error_code == "validation_error"

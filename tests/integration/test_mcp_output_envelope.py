import asyncio
import json
import uuid

import pytest

from docket.config import get_settings
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.mcp.server import mcp
from docket.models import Account
from docket.services.calendar_lanes import CalendarLaneService


@pytest.mark.integration
def test_mcp_boundary_minifies_null_omits_and_byte_bounds_default_lists(
    session_factory,
) -> None:
    del session_factory
    server = ProvenanceFastMCP("output-envelope-test", caller_profile="interactive")

    @server.tool(name="docket_search_history")
    def search_history() -> dict[str, object]:
        return {
            "ok": True,
            "results": [
                {
                    "ref": f"entry-{index}",
                    "summary": "ø" * 2000,
                    "account_id": str(uuid.uuid4()),
                    "calendar_id": str(uuid.uuid4()),
                    "nested": {"id": str(uuid.uuid4())},
                    "unused": None,
                }
                for index in range(100)
            ],
            "cursor": "next-page",
            "optional": None,
        }

    result = asyncio.run(server.call_tool("docket_search_history", {}))
    assert isinstance(result, tuple) and len(result) == 2
    content, structured = result
    assert isinstance(content, list) and len(content) == 1
    text = content[0].text
    assert isinstance(text, str)
    payload = json.loads(text)
    assert structured == payload
    assert len(text.encode("utf-8")) <= 16384
    assert "results" not in payload
    assert payload["count"] == len(payload["items"])
    assert payload["count"] <= 25
    assert payload["total_if_known"] == 100
    assert payload["truncated"] is True
    assert payload["cursor"] == "next-page"
    assert "optional" not in payload
    assert all("unused" not in item for item in payload["items"])
    assert all("account_id" not in item for item in payload["items"])
    assert all("id" not in item["nested"] for item in payload["items"])
    assert all("calendar_id" in item for item in payload["items"])


@pytest.mark.integration
def test_explicit_audit_view_uses_64_kib_utf8_budget_and_self_truncates(
    session_factory,
) -> None:
    del session_factory
    server = ProvenanceFastMCP("audit-output-envelope-test", caller_profile="interactive")

    @server.tool(name="docket_search_history")
    def search_history(view: str = "summary") -> dict[str, object]:
        return {
            "ok": True,
            "view": view,
            "items": [{"ref": f"entry-{index}", "summary": "界" * 1000} for index in range(100)],
            "count": 100,
            "total_if_known": 100,
            "truncated": False,
            "cursor": "audit-next-page",
        }

    result = asyncio.run(server.call_tool("docket_search_history", {"view": "audit"}))
    assert isinstance(result, tuple) and len(result) == 2
    content, structured = result
    assert isinstance(content, list) and len(content) == 1
    text = content[0].text
    assert isinstance(text, str)
    payload = json.loads(text)
    assert structured == payload
    assert 16384 < len(text.encode("utf-8")) <= 65536
    assert payload["count"] == len(payload["items"])
    assert payload["count"] <= 25
    assert payload["count"] < 100
    assert payload["truncated"] is True
    assert payload["cursor"] == "audit-next-page"


@pytest.mark.integration
def test_retained_provider_reads_chain_through_public_refs_without_uuid_leak(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="mcp-provider-ref-test",
            display_name="MCP provider ref",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        account_ref = account.ref_id
        CalendarLaneService(session, get_settings()).ensure_for_account(account)

    accounts_result = asyncio.run(mcp.call_tool("docket_list_accounts", {}))
    assert isinstance(accounts_result, tuple)
    accounts = accounts_result[1]
    assert accounts["items"][0]["ref"] == account_ref
    assert "account_id" not in accounts["items"][0]
    assert accounts["items"][0]["calendar_lanes"]
    assert all(
        lane["ref"].startswith("lane_") and "lane_id" not in lane and "account_id" not in lane
        for lane in accounts["items"][0]["calendar_lanes"]
    )

    lanes_result = asyncio.run(
        mcp.call_tool("docket_list_calendar_lanes", {"account_ref": account_ref})
    )
    assert isinstance(lanes_result, tuple)
    lanes = lanes_result[1]
    assert lanes["items"]
    assert all(item["ref"].startswith("lane_") for item in lanes["items"])
    assert all("lane_id" not in item and "account_id" not in item for item in lanes["items"])

"""Exercise the containerized health endpoint and authenticated MCP boundary."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "docket_store_record",
    "docket_get_record",
    "docket_search_records",
    "docket_update_record",
    "docket_archive_record",
    "docket_list_accounts",
    "docket_propose_action",
    "docket_get_action",
}


def _token() -> str:
    credentials_dir = Path(os.environ.get("DOCKET_CREDENTIALS_DIR", "secrets/smoke"))
    return (credentials_dir / "docket_to_hermes_token").read_text(encoding="utf-8").strip()


async def smoke() -> None:
    base_url = os.environ.get("DOCKET_SMOKE_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"Authorization": f"Bearer {_token()}"}
    async with httpx.AsyncClient(timeout=15) as anonymous_client:
        unauthorized = await anonymous_client.post(f"{base_url}/mcp/", json={})
        assert unauthorized.status_code == 401

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        health = await client.get(f"{base_url}/health/ready")
        health.raise_for_status()
        body = health.json()
        assert body["status"] == "ok"
        assert body["credential_mode"] == "dummy"
        assert body["google_oauth"] == "dummy"
        assert body["external_calls_enabled"] is False

        provider = await client.get(f"{base_url}/health/smoke-provider")
        provider.raise_for_status()
        assert provider.json()["status"] == "ok"

        async with streamable_http_client(f"{base_url}/mcp/", http_client=client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == EXPECTED_TOOLS, names

                stored = await session.call_tool(
                    "docket_store_record",
                    {
                        "record_type": "term",
                        "canonical_identity": {
                            "institution": "Docket Smoke University",
                            "term_name": "Fall 2099",
                        },
                        "title": "Fall 2099",
                        "data": {
                            "institution": "Docket Smoke University",
                            "term_name": "Fall 2099",
                            "start_date": "2099-08-24",
                            "end_date": "2099-12-18",
                            "timezone": "America/Los_Angeles",
                            "notes": "Dummy Compose smoke record",
                        },
                        "request_key": (
                            "discord:000000000000000002:000000000000000003:"
                            "999999999999999999:0"
                        ),
                        "source": {
                            "source_type": "discord_message",
                            "source_object_id": "999999999999999999",
                            "metadata": {
                                "guild_id": "000000000000000002",
                                "channel_id": "000000000000000003",
                                "message_id": "999999999999999999",
                                "user_id": "000000000000000001",
                                "intent_index": 0,
                            },
                        },
                        "actor_id": "000000000000000001",
                    },
                )
                assert not stored.isError, stored

                searched = await session.call_tool(
                    "docket_search_records",
                    {"record_type": "term", "query": "Fall 2099", "limit": 5},
                )
                assert not searched.isError, searched

    print("Compose MCP smoke passed: dummy provider, auth, allowlist, create, and search")


if __name__ == "__main__":
    asyncio.run(smoke())

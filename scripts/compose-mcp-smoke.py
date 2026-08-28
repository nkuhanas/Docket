"""Exercise the containerized health endpoint and authenticated MCP boundary."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "docket_get_record",
    "docket_search_records",
    "docket_list_accounts",
    "docket_list_calendar_lanes",
    "docket_list_calendar_events",
    "docket_get_calendar_sync_status",
    "docket_get_calendar_profile",
    "docket_list_reminder_rules",
    "docket_list_queue_items",
    "docket_get_queue_item",
    "docket_get_triage_case",
    "docket_search_history",
    "docket_get_history_entry",
    "docket_get_conflict",
    "docket_get_intent_session",
    "docket_get_network_neighborhood",
    "docket_get_organization_context",
    "docket_get_person_context",
    "docket_network_search",
    "docket_query_people",
    "docket_commit_changeset",
    "docket_resolve_conflict",
}
EXPECTED_TRIAGE_TOOLS = {
    "docket_get_triage_context",
    "docket_submit_triage_analysis",
    "docket_get_triage_case",
    "docket_apply_existing_suppression",
}


def _token() -> str:
    credentials_dir = Path(os.environ.get("DOCKET_CREDENTIALS_DIR", "secrets/smoke"))
    return (credentials_dir / "docket_to_hermes_token").read_text(encoding="utf-8").strip()


def _service_token() -> str:
    credentials_dir = Path(os.environ.get("DOCKET_CREDENTIALS_DIR", "secrets/smoke"))
    return (credentials_dir / "hermes_to_docket_token").read_text(encoding="utf-8").strip()


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
        assert body["calendar_reads_enabled"] is False
        assert body["external_writes_enabled"] is False
        assert body["gmail_ingestion_enabled"] is False
        assert body["gmail_writes_enabled"] is False

        provider = await client.get(f"{base_url}/health/smoke-provider")
        provider.raise_for_status()
        assert provider.json()["status"] == "ok"

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_service_token()}"},
            timeout=15,
        ) as service_client:
            utterance = await service_client.post(
                f"{base_url}/internal/v1/discord/operator-utterances",
                json={
                    "request_id": str(uuid.uuid4()),
                    "guild_id": "000000000000000002",
                    "channel_id": "000000000000000003",
                    "message_id": "999999999999999999",
                    "actor_id": "000000000000000001",
                    "verbatim_text": "Store the dummy Compose smoke term.",
                    "request_key": (
                        "discord:000000000000000002:000000000000000003:"
                        "999999999999999999:0"
                    ),
                },
            )
            utterance.raise_for_status()
            assert utterance.json()["ref"].startswith("utt_")

        async with streamable_http_client(f"{base_url}/mcp/", http_client=client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == EXPECTED_TOOLS, names

                searched = await session.call_tool(
                    "docket_search_history",
                    {"object_type": "operator_utterance", "limit": 5},
                )
                assert not searched.isError, searched

        async with streamable_http_client(
            f"{base_url}/triage-mcp/",
            http_client=client,
        ) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                triage_tools = await session.list_tools()
                triage_names = {tool.name for tool in triage_tools.tools}
                assert triage_names == EXPECTED_TRIAGE_TOOLS, triage_names

    print(
        "Compose MCP smoke passed: dummy provider, auth, isolated allowlists, "
        "provenance capture, and bounded history search"
    )


if __name__ == "__main__":
    asyncio.run(smoke())

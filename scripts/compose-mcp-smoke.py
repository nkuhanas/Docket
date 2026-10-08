"""Verify isolated authenticated MCP profiles and request-backed work over HTTP."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from docket.tool_contracts import contract_tool_names


def _token(filename: str) -> str:
    root = Path(os.environ.get("DOCKET_CREDENTIALS_DIR", "secrets/smoke"))
    return (root / filename).read_text(encoding="utf-8").strip()


async def smoke() -> None:
    base = os.environ.get("DOCKET_SMOKE_URL", "http://127.0.0.1:18080").rstrip("/")
    foreground = {"Authorization": f"Bearer {_token('docket_to_hermes_token')}"}
    triage = {"Authorization": f"Bearer {_token('docket_triage_token')}"}
    service = {"Authorization": f"Bearer {_token('hermes_to_docket_token')}"}
    async with httpx.AsyncClient(timeout=15) as client:
        live = await client.get(f"{base}/health/live")
        live.raise_for_status()
        ready = await client.get(f"{base}/health/ready")
        ready.raise_for_status()
        for path, headers in (("mcp", {}), ("mcp", triage), ("triage-mcp", foreground)):
            denied = await client.post(f"{base}/{path}/", headers=headers, json={})
            assert denied.status_code == 401, (path, denied.status_code)
        for path, headers, profile in (
            ("mcp", foreground, "interactive"),
            ("triage-mcp", triage, "triage"),
        ):
            async with (
                httpx.AsyncClient(headers=headers, timeout=30) as mcp_client,
                streamable_http_client(f"{base}/{path}/", http_client=mcp_client) as transport,
                ClientSession(transport[0], transport[1]) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == set(contract_tool_names(profile))
                if profile == "interactive":
                    commit = next(t for t in tools.tools if t.name == "docket_commit_changeset")
                    assert all(
                        p.get("x-docket-internal")
                        for p in commit.inputSchema["properties"].values()
                    )

        async def call(
            name: str, args: dict[str, Any], operation: str, *, execution: str = "smoke-foreground"
        ) -> dict[str, Any]:
            headers = {
                **foreground,
                "x-docket-request-key": "smoke:authenticated-work",
                "x-docket-execution-key": execution,
                "x-docket-operation-key": operation,
            }
            async with (
                httpx.AsyncClient(headers=headers, timeout=30) as mcp_client,
                streamable_http_client(f"{base}/mcp/", http_client=mcp_client) as transport,
                ClientSession(transport[0], transport[1]) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, args)
                payload = json.loads(result.content[0].text)
                assert len(json.dumps(payload).encode()) <= 16 * 1024
                return payload

        no_context = {**foreground, "x-docket-operation-key": "missing-request"}
        async with (
            httpx.AsyncClient(headers=no_context, timeout=30) as mcp_client,
            streamable_http_client(f"{base}/mcp/", http_client=mcp_client) as transport,
            ClientSession(transport[0], transport[1]) as session,
        ):
            await session.initialize()
            rejected = await session.call_tool("docket_commit_changeset", {})
            assert json.loads(rejected.content[0].text)["error"]["code"] == (
                "request_context_required"
            )
        admitted = await client.post(
            f"{base}/internal/v1/agent/requests",
            headers=service,
            json={"request_key": "smoke:authenticated-work", "execution_key": "smoke-foreground"},
        )
        admitted.raise_for_status()
        admitted_request = admitted.json()
        assert admitted_request["request_ref"].startswith("req_")
        duplicate = await client.post(
            f"{base}/internal/v1/agent/requests",
            headers=service,
            json={"request_key": "smoke:authenticated-work", "execution_key": "duplicate"},
        )
        duplicate.raise_for_status()
        assert duplicate.json()["execution_disposition"] == "already_dispatched"
        background_admission = await client.post(
            f"{base}/internal/v1/agent/requests",
            headers=triage,
            json={"request_key": "copied-authority"},
        )
        assert background_admission.status_code == 401
        staged = await call(
            "docket_stage_changes",
            {
                "assembly_scope": {
                    "resolved_intent": {"intent": "track synthetic requested work"},
                    "allowed_mutation_types": ["item_create"],
                    "planned_create_types": ["item"],
                },
                "patch": {
                    "operations": [
                        {
                            "operation": "action_upsert",
                            "action": {
                                "change_id": "smoke-item",
                                "mutation_type": "item_create",
                                "action": "create",
                                "object_type": "item",
                                "affected_fields": ["title"],
                                "create_spec": {
                                    "title": "Synthetic requested work",
                                    "kind": "test.smoke",
                                },
                            },
                        }
                    ]
                },
            },
            "stage",
        )
        assert staged["disposition"] == "ready_to_commit", staged
        stale = await call("docket_commit_changeset", {}, "stale", execution="unobserved-execution")
        assert stale["disposition"] != "committed", stale
        reviewed = await call("docket_review_changeset", {}, "review")
        assert reviewed["ok"], reviewed
        committed = await call("docket_commit_changeset", {}, "commit")
        assert committed["disposition"] == "committed", committed
        replay = await call("docket_commit_changeset", {}, "commit")
        assert replay["changeset_ref"] == committed["changeset_ref"]
        requests = await call(
            "docket_search_history",
            {
                "object_type": "authenticated_request",
                "limit": 5,
            },
            "history",
        )
        request_ref = requests["items"][0]["ref"]
        recorded = await client.post(
            f"{base}/internal/v1/agent/records",
            headers=service,
            json={
                "request_ref": request_ref,
                "record_key": "late-capture-gap",
                "record_kind": "operator_transcript",
                "gap_code": "synthetic_archive_unavailable",
            },
        )
        recorded.raise_for_status()
        history = await call("docket_get_history_entry", {"ref": request_ref}, "request-history")
        assert history["entry"]["state"] == "committed", history
        assert history["entry"]["capture_status"] == "gaps_recorded", history
        calls = await call(
            "docket_search_history",
            {
                "object_type": "tool_invocation",
                "limit": 25,
            },
            "calls",
        )
        assert any(row.get("error_code") == "request_context_required" for row in calls["items"])
        completed = await client.post(
            f"{base}/internal/v1/agent/executions/complete",
            headers=service,
            json={
                "request_ref": admitted_request["request_ref"],
                "execution_key": "smoke-foreground",
                "completion_token": admitted_request["completion_token"],
            },
        )
        completed.raise_for_status()
    print(
        json.dumps(
            {
                "status": "passed",
                "interactive_tools": 23,
                "triage_tools": 4,
                "request_commit_replay": True,
                "capture_gap_after_commit": True,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(smoke())

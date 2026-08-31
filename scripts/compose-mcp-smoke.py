"""Exercise the containerized health endpoint and authenticated MCP boundary."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from docket.domain.canonical import sha256_json
from docket.domain.public_refs import new_public_ref
from docket.tool_contracts import CONTRACT_VERSION, contract_hash

EXPECTED_TOOLS = {
    "docket_commit_changeset",
    "docket_get_attention_case",
    "docket_get_calendar_sync_status",
    "docket_get_conflict",
    "docket_get_context_neighborhood",
    "docket_get_history_entry",
    "docket_get_intent_session",
    "docket_get_item_context",
    "docket_get_organization_or_institution_context",
    "docket_get_person_context",
    "docket_list_calendar_lanes",
    "docket_list_provider_accounts",
    "docket_list_provider_calendar_events",
    "docket_list_reminder_plans",
    "docket_query_items",
    "docket_query_people",
    "docket_resolve_conflict",
    "docket_search_entities",
    "docket_search_history",
}
EXPECTED_TRIAGE_TOOLS = {
    "docket_get_triage_context",
    "docket_submit_triage_analysis",
    "docket_get_attention_case",
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
            gateway_registration_key = str(uuid.uuid4())
            gateway_responses = await asyncio.gather(
                service_client.post(
                    f"{base_url}/internal/v1/discord/gateway-lifetimes",
                    json={
                        "request_id": str(uuid.uuid4()),
                        "registration_key": gateway_registration_key,
                        "instance_kind": "hermes_discord_gateway",
                    },
                ),
                service_client.post(
                    f"{base_url}/internal/v1/discord/gateway-lifetimes",
                    json={
                        "request_id": str(uuid.uuid4()),
                        "registration_key": gateway_registration_key,
                        "instance_kind": "hermes_discord_gateway",
                    },
                ),
            )
            for gateway_response in gateway_responses:
                gateway_response.raise_for_status()
            gateway_results = [response.json() for response in gateway_responses]
            assert gateway_results[0]["ref"] == gateway_results[1]["ref"]
            assert {result["disposition"] for result in gateway_results} == {
                "created",
                "replayed_request",
            }

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
            utterance_ref = utterance.json()["ref"]
            assert utterance_ref.startswith("utt_")

            source = {
                "source_type": "discord_message",
                "source_object_id": "999999999999999999",
                "metadata": {
                    "guild_id": "000000000000000002",
                    "channel_id": "000000000000000003",
                    "message_id": "999999999999999999",
                    "user_id": "000000000000000001",
                    "intent_index": 1,
                },
            }
            changeset_arguments = {
                "utterance_ref": utterance_ref,
                "statements": [],
                "relations": [],
                "resolved_intent": {"kind": "compose_smoke"},
                "blocking_clarifications": [
                    {"blocking": True, "question": "Which dummy term?"}
                ],
                "content": None,
                "request_key": (
                    "discord:000000000000000002:000000000000000003:"
                    "999999999999999999:1"
                ),
                "source": source,
                "actor_id": "000000000000000001",
            }

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

                clarification = await session.call_tool(
                    "docket_commit_changeset",
                    changeset_arguments,
                )
                assert not clarification.isError, clarification

        trace_ref = new_public_ref("trace")
        argument_hash = sha256_json(changeset_arguments)
        turn_started_at = datetime.now(UTC).isoformat()
        trace_context = {
            "guild_id": "000000000000000002",
            "source_channel_id": "000000000000000003",
            "source_message_id": "999999999999999999",
            "actor_id": "000000000000000001",
            "tool_contract_version": CONTRACT_VERSION,
            "tool_contract_hash": contract_hash("interactive"),
            "caller_profile": "interactive",
            "turn_started_at": turn_started_at,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        running_call = {
            "call_id": "compose-smoke-call",
            "ordinal": 1,
            "tool_name": "docket_commit_changeset",
            "transport_state": "running",
            "received_argument_hash": argument_hash,
            "argument_preview": '{"fields":["content"]}',
        }
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_service_token()}"},
            timeout=15,
        ) as service_client:
            for call, turn_status in (
                (running_call, "running"),
                (
                    {
                        **running_call,
                        "transport_state": "completed",
                        "elapsed_ms": 1,
                        "disposition": "succeeded",
                    },
                    "running",
                ),
                (None, "completed"),
            ):
                trace = await service_client.put(
                    f"{base_url}/internal/v1/discord/mcp-traces/{trace_ref}",
                    json={
                        **trace_context,
                        "request_id": str(uuid.uuid4()),
                        "turn_status": turn_status,
                        "call": call,
                    },
                )
                trace.raise_for_status()

            response = await service_client.post(
                f"{base_url}/internal/v1/discord/agent-responses",
                json={
                    "request_id": str(uuid.uuid4()),
                    "guild_id": "000000000000000002",
                    "channel_id": "000000000000000003",
                    "source_message_id": "999999999999999999",
                    "actor_id": "000000000000000001",
                    "utterance_ref": utterance_ref,
                    "turn_id": "compose-smoke-turn",
                    "session_id": "compose-smoke-session",
                    "model_identifier": "compose-smoke-model",
                    "verbatim_text": "Which dummy term did you mean?",
                    "generated_at": datetime.now(UTC).isoformat(),
                    "trace_ref": trace_ref,
                },
            )
            response.raise_for_status()
            response_ref = response.json()["ref"]
            delivery = await service_client.put(
                f"{base_url}/internal/v1/discord/agent-responses/{response_ref}/delivery",
                json={
                    "request_id": str(uuid.uuid4()),
                    "response_ref": response_ref,
                    "guild_id": "000000000000000002",
                    "channel_id": "000000000000000003",
                    "source_message_id": "999999999999999999",
                    "actor_id": "000000000000000001",
                    "outcome": "delivered",
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
            delivery.raise_for_status()
            assert delivery.json()["state"] == "delivered"

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
        "provenance capture, response finalization, and bounded history search"
    )


if __name__ == "__main__":
    asyncio.run(smoke())

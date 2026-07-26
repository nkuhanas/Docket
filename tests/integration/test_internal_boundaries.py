import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from docket.config import get_settings
from docket.main import app
from docket.services.mcp_traces import trace_id_for_source


@pytest.mark.integration
def test_internal_api_and_mcp_require_distinct_tokens(session_factory) -> None:
    settings = get_settings()
    payload = {
        "request_id": str(uuid.uuid4()),
        "discord_interaction_id": "message:123",
        "approval_id": None,
        "approval_token": None,
        "short_code": "ABCDEFGH",
        "decision": "approve",
        "discord_user_id": settings.operator_discord_user_id,
        "guild_id": settings.discord_guild_id,
        "channel_id": settings.queue_channel_id,
        "message_id": "123",
        "responded_at": datetime.now(UTC).isoformat(),
    }
    with TestClient(app) as client:
        assert (
            client.post("/internal/v1/discord/approval-responses", json=payload).status_code == 401
        )
        assert (
            client.post(
                "/internal/v1/discord/approval-responses",
                json=payload,
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        authenticated = client.post(
            "/internal/v1/discord/approval-responses",
            json=payload,
            headers={"Authorization": f"Bearer {settings.hermes_to_docket_token()}"},
        )
        assert authenticated.status_code == 404
        assert authenticated.json()["detail"]["code"] == "approval_not_found"

        trace_message_id = "777777777777777777"
        trace_id = trace_id_for_source(
            settings.discord_guild_id,
            settings.chat_channel_id,
            trace_message_id,
        )
        trace_payload = {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "source_channel_id": settings.chat_channel_id,
            "source_message_id": trace_message_id,
            "actor_id": settings.operator_discord_user_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "turn_status": "running",
            "call": {
                "call_id": "call-1",
                "ordinal": 1,
                "tool_name": "docket_search_records",
                "state": "running",
                "elapsed_ms": 0,
                "disposition": None,
                "error_code": None,
            },
        }
        trace_path = f"/internal/v1/discord/mcp-traces/{trace_id}"
        assert client.put(trace_path, json=trace_payload).status_code == 401
        accepted_trace = client.put(
            trace_path,
            json=trace_payload,
            headers={"Authorization": f"Bearer {settings.hermes_to_docket_token()}"},
        )
        assert accepted_trace.status_code == 200
        assert accepted_trace.json()["trace_version"] == 1

        assert client.get("/mcp").status_code == 401
        mcp_response = client.get(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {settings.docket_to_hermes_token()}",
                "Host": "docket:8000",
            },
        )
        assert mcp_response.status_code not in {401, 421}

        rejected_host = client.get(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {settings.docket_to_hermes_token()}",
                "Host": "attacker.example",
            },
        )
        assert rejected_host.status_code == 421

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["calendar_reads_enabled"] is False
        assert ready.json()["external_writes_enabled"] is False
        assert ready.json()["google_oauth"] == "dummy"

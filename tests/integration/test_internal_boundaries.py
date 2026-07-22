import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from docket.config import get_settings
from docket.main import app


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
        assert ready.json()["external_calls_enabled"] is False
        assert ready.json()["google_oauth"] == "dummy"

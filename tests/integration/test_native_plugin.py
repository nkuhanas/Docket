import json
import time

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from docket.config import get_settings
from docket.models import AuthenticatedRequest, ToolInvocation
from docket.plugin.app import create_plugin_app
from docket.services.oauth import OAuthPrincipalService, OAuthVerifier
from docket.tool_contracts import contract_tool_names


@pytest.fixture
def linked_plugin(session_factory):
    settings = get_settings().model_copy(update={
        "plugin_enabled": True, "plugin_oauth_issuer": "https://identity.test/",
        "plugin_oauth_jwks_url": "https://identity.test/.well-known/jwks.json",
        "plugin_oauth_client_ids": ["native-client"],
    })
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    verifier = OAuthVerifier(settings, httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"keys": [jwk]}),
    )))
    now = int(time.time())
    token = jwt.encode({
        "iss": settings.plugin_oauth_issuer, "sub": "owner", "azp": "native-client",
        "aud": settings.plugin_public_origin, "iat": now, "exp": now + 3600,
        "scope": "docket.read docket.events write commit",
    }, key, algorithm="RS256", headers={"kid": "test-key"})
    with session_factory.begin() as session:
        ref = OAuthPrincipalService(session, settings).approve("owner", events=True).ref_id
    app = create_plugin_app(settings, verifier=verifier, factory=session_factory)
    return app, settings, token, ref


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_linked_plugin_schema_profile_audit_and_no_background_writes(
    session_factory, linked_plugin, mode,
):
    app, settings, token, ref = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": f"Bearer {token}",
                     "x-docket-request-key": "copied-foreground",
                     "x-docket-execution-key": "copied-execution"},
        ) as http,
        Client(streamable_http_client(
            "https://docket.you/mcp/", http_client=http,
        ), mode=mode) as client,
    ):
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == contract_tool_names("plugin")
        assert len(tools) == 19
        assert all(tool.annotations.read_only_hint for tool in tools)
        assert all(not any(
            value.get("x-docket-internal") for value in tool.input_schema["properties"].values()
        ) for tool in tools)
        profile = next(tool for tool in tools if tool.name == "docket_get_profile")
        assert profile.meta["openai/profile"] is True
        assert profile.meta["securitySchemes"] == [{
            "type": "oauth2", "scopes": [settings.plugin_read_scope],
        }]
        first = await client.call_tool("docket_get_profile", {})
        assert first.structured_content == {"id": ref, "name": "Docket"}
        denied = await client.call_tool("docket_commit_changeset", {})
        assert denied.structured_content["error"]["code"] == "agent_authority_denied"
        with session_factory() as session:
            calls = session.scalars(select(ToolInvocation)).all()
            assert len(calls) == 2
            assert all(call.actor_ref == ref for call in calls)
            assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 0
        with session_factory.begin() as session:
            OAuthPrincipalService(session, settings).revoke(ref)
        response = await http.post("https://docket.you/mcp/", json={
            "jsonrpc": "2.0", "id": "revoked", "method": "tools/list", "params": {},
        })
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_public_app_exposes_only_connection_and_discovery(linked_plugin):
    app, settings, _, _ = linked_plugin
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http:
        resource = await http.get("https://docket.you/.well-known/oauth-protected-resource")
        assert resource.status_code == 200
        assert resource.json()["resource"] == settings.plugin_public_origin
        for path in (
            "/internal/v1/agent/requests", "/triage-mcp/", "/health/ready", "/openapi.json",
        ):
            assert (await http.get(f"https://docket.you{path}")).status_code == 404
        for token in ("", get_settings().docket_to_hermes_token(),
                      get_settings().hermes_to_docket_token()):
            response = await http.post("https://docket.you/mcp/", json={}, headers={
                "Authorization": f"Bearer {token}",
            })
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_disabled_public_plugin_returns_no_metadata(session_factory):
    app = create_plugin_app(get_settings(), factory=session_factory)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http:
        response = await http.get("https://docket.you/.well-known/oauth-protected-resource")
        assert response.status_code == 404

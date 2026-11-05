"""Boot the actual application without any Hermes/Discord service or credentials."""

import asyncio
import json
import time

import httpx
import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from docket.agent_auth import TRIAGE_WORK_TOOLS
from docket.config import get_settings
from docket.database import session_scope
from docket.domain.enums import Environment
from docket.mcp.instrumented import INTERACTIVE_AUTHORITY_TOOLS
from docket.models import AuthenticatedRequest, Item, ToolInvocation
from docket.services.oauth import OAuthPrincipalService

settings = get_settings()
assert settings.environment is Environment.TEST
assert settings.plugin_enabled and not settings.discord_adapter_enabled
assert not settings.interactive_agent_enabled
assert not settings.external_writes_enabled and not settings.calendar_reads_enabled
assert not settings.gmail_ingestion_enabled

from docket.ingress_credentials import main as provision_ingress  # noqa: E402
from docket.main import app, plugin_app, worker  # noqa: E402


async def check() -> None:
    provision_ingress()  # Native startup does not open an ingress password file.
    assert worker.discord_projection_runner is None
    assert worker.deferred_ingress_runner is None
    assert worker.gateway_lifetime_reconciler is None
    assert worker.notification_dispatcher is not None
    assert worker.triage_inbox_service is not None
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http,
    ):
        metadata = await http.get(
            "http://docket:8000/plugin/.well-known/oauth-protected-resource",
        )
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == "https://docket.you"
        assert (await http.post("http://docket:8000/plugin/mcp/", json={})).status_code == 401
        assert (await http.get("http://docket:8000/plugin/triage-mcp/")).status_code == 404
        tools = await plugin_app.state.mcp.list_tools()
        assert len(tools) == 30
        assert all(tool.annotations.read_only_hint is (
            tool.name not in TRIAGE_WORK_TOOLS | INTERACTIVE_AUTHORITY_TOOLS)
                   for tool in tools)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="native-smoke-key", alg="RS256", use="sig")
        verifier = plugin_app.user_middleware[0].kwargs["verifier"]
        verifier.client.close()
        verifier.client = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"keys": [jwk]}),
        ))
        with session_scope() as session:
            principal_ref = OAuthPrincipalService(session, settings).approve(
                "native-smoke-owner", write=True, events=True, triage=True,
            ).ref_id
        now = int(time.time())
        credential = jwt.encode({
            "iss": settings.plugin_oauth_issuer, "aud": settings.plugin_public_origin,
            "sub": "native-smoke-owner", "azp": settings.plugin_oauth_client_ids[0],
            "iat": now, "exp": now + 3600,
            "scope": " ".join((settings.plugin_read_scope, settings.plugin_write_scope,
                               settings.plugin_triage_scope, settings.plugin_events_scope)),
        }, key, algorithm="RS256", headers={"kid": "native-smoke-key"})
        http.headers["Authorization"] = "Bearer " + credential
        def work(operation: str) -> dict[str, str]:
            return {"request_id": "native-smoke", "execution_id": "scheduled-run",
                    "operation_id": operation}

        async with Client(streamable_http_client(
            "http://docket:8000/plugin/mcp/", http_client=http,
        ), mode="legacy") as client:
            assert len((await client.list_tools()).tools) == 30
            staged = (await client.call_tool("docket_stage_changes", {
                "work": work("stage"), "assembly_scope": {
                    "resolved_intent": {"intent": "track the synthetic smoke item"},
                    "allowed_mutation_types": ["item_create"], "planned_create_types": ["item"],
                }, "patch": {"operations": [{"operation": "action_upsert", "action": {
                    "change_id": "item-1", "mutation_type": "item_create", "action": "create",
                    "object_type": "item", "affected_fields": ["title"],
                    "create_spec": {"title": "Native smoke item", "kind": "test.smoke"},
                }}]},
            })).structured_content
            assert staged and staged["disposition"] == "ready_to_commit", staged
            committed = (await client.call_tool("docket_commit_changeset", {
                "work": work("commit"),
            })).structured_content
            assert committed and committed["disposition"] == "committed", committed
        with session_scope() as session:
            root = session.scalar(select(AuthenticatedRequest).where(
                AuthenticatedRequest.principal_ref == principal_ref,
            ))
            assert root and root.state == "committed" and root.source_utterance_ref is None
            item = session.scalar(select(Item).where(Item.title == "Native smoke item"))
            assert item and root.ref_id in item.basis_refs
            calls = list(session.scalars(select(ToolInvocation).where(
                ToolInvocation.actor_ref == principal_ref,
            )))
            assert len(calls) == 2 and all(row.domain_state == "succeeded" for row in calls)
            OAuthPrincipalService(session, settings).revoke(principal_ref)
        assert (await http.post("http://docket:8000/plugin/mcp/", json={})).status_code == 401
        await asyncio.sleep(0)
        assert worker.is_healthy()
    assert worker._task is None and worker._notification_task is None
    print(json.dumps({"native_only_startup": "passed", "tools": len(tools),
                      "inference_provider": "caller_owned", "delegated_write": "passed"}))


asyncio.run(check())

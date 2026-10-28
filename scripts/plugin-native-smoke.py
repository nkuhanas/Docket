"""Boot the actual application without any Hermes/Discord service or credentials."""

import asyncio
import json

import httpx2

from docket.agent_auth import TRIAGE_WORK_TOOLS
from docket.config import get_settings
from docket.domain.enums import Environment

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
        assert len(tools) == 24
        assert all(tool.annotations.read_only_hint is (tool.name not in TRIAGE_WORK_TOOLS)
                   for tool in tools)
        await asyncio.sleep(0)
        assert worker.is_healthy()
    assert worker._task is None and worker._notification_task is None
    print(json.dumps({"native_only_startup": "passed", "tools": len(tools),
                      "inference_provider": "caller_owned"}))


asyncio.run(check())

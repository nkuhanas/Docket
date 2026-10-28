"""An agent-neutral facade with separately permitted reads and intelligence work."""

from copy import deepcopy
from typing import Annotated, Any
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool, ToolAnnotations
from pydantic import Field

from docket.agent_auth import TRIAGE_WORK_TOOLS, require_principal
from docket.config import Settings
from docket.database import session_scope
from docket.mcp.instrumented import INFRASTRUCTURE_ARGUMENT_NAMES, ProvenanceMCP
from docket.mcp.server import mcp
from docket.mcp.triage_server import triage_mcp
from docket.plugin.events import EventMethods, advertise_events
from docket.providers.webhooks import WebhookPoster
from docket.services.notifications import NotificationService
from docket.tool_contracts import contract_tool_names, render_contract


class PluginMCP(ProvenanceMCP):
    def __init__(self, settings: Settings, poster: WebhookPoster | None = None) -> None:
        hostname = urlsplit(settings.plugin_public_origin).hostname or "docket.you"
        super().__init__(
            "Docket", caller_profile="interactive", contract_profile="plugin",
            instructions=render_contract("plugin"),
            extensions=[EventMethods(settings, poster)], middleware=[advertise_events],
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[hostname, hostname + ":443", "127.0.0.1:*", "docket:8000"],
                allowed_origins=[settings.plugin_public_origin.rstrip("/")],
            ),
        )
        self.plugin_settings = settings
        annotations = ToolAnnotations(
            read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        )
        names = contract_tool_names("plugin")
        for info in mcp._tool_manager.list_tools():
            if info.name in names:
                self.add_tool(info.fn, name=info.name, description=info.description,
                              annotations=annotations)
        for info in triage_mcp._tool_manager.list_tools():
            if info.name in TRIAGE_WORK_TOOLS:
                self.add_tool(info.fn, name=info.name, description=info.description,
                              annotations=ToolAnnotations(
                                  read_only_hint=False, destructive_hint=False,
                                  idempotent_hint=False, open_world_hint=False,
                              ))

        @self.tool(annotations=annotations)
        def docket_get_profile() -> dict[str, Any]:
            """Return the stable approved profile represented by the connection."""
            principal = require_principal("read")
            return {"id": principal.principal_ref, "name": "Docket"}

        profile = self._tool_manager.get_tool("docket_get_profile")
        assert profile is not None
        profile.fn_metadata.output_schema = {
            "type": "object", "properties": {
                "id": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "name": {"type": "string"},
            }, "required": ["id"], "additionalProperties": False,
        }

        @self.tool(annotations=annotations)
        def docket_list_notifications(
            limit: Annotated[int, Field(ge=1, le=100)] = 25,
            cursor: Annotated[str | None, Field(max_length=256)] = None,
        ) -> dict[str, Any]:
            """Read retained notifications without acknowledging operator visibility."""
            with session_scope() as session:
                return NotificationService(session, self.plugin_settings).list(
                    limit=limit, cursor=cursor,
                )

        @self.tool(annotations=annotations)
        def docket_get_notification(notification_ref: str) -> dict[str, Any]:
            """Read an inbox record and follow its public subject for current canonical context."""
            with session_scope() as session:
                return NotificationService(session, self.plugin_settings).get(notification_ref)

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = deepcopy(tool.input_schema)
            properties = tool.input_schema.get("properties", {})
            hidden = {
                key for key, value in properties.items()
                if key in INFRASTRUCTURE_ARGUMENT_NAMES or value.get("x-docket-internal")
            }
            for key in hidden:
                properties.pop(key, None)
            if "required" in tool.input_schema:
                tool.input_schema["required"] = [
                    key for key in tool.input_schema["required"] if key not in hidden
                ]
            scopes = [self.plugin_settings.plugin_read_scope]
            if tool.name in TRIAGE_WORK_TOOLS:
                scopes.append(self.plugin_settings.plugin_triage_scope)
            tool.meta = {
                **(tool.meta or {}),
                "securitySchemes": [{"type": "oauth2", "scopes": scopes}],
            }
            if tool.name == "docket_get_profile":
                tool.meta["openai/profile"] = True
        return tools

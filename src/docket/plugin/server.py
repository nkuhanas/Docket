"""A native read facade using the same audited business handlers."""

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool, ToolAnnotations

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.mcp.instrumented import INFRASTRUCTURE_ARGUMENT_NAMES, ProvenanceMCP
from docket.mcp.server import mcp
from docket.tool_contracts import contract_tool_names, render_contract


class PluginMCP(ProvenanceMCP):
    def __init__(self, settings: Settings) -> None:
        hostname = urlsplit(settings.plugin_public_origin).hostname or "docket.you"
        super().__init__(
            "Docket", caller_profile="interactive", contract_profile="plugin",
            instructions=render_contract("plugin"),
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
            tool.meta = {
                **(tool.meta or {}),
                "securitySchemes": [{"type": "oauth2", "scopes": [
                    self.plugin_settings.plugin_read_scope,
                ]}],
            }
            if tool.name == "docket_get_profile":
                tool.meta["openai/profile"] = True
        return tools

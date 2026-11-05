"""Native delegated operations, separately scoped from reads, triage and events."""

import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import Annotated, Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy import select

from docket.agent_auth import (
    TRIAGE_WORK_TOOLS,
    AgentCallContext,
    agent_call_context,
    require_principal,
)
from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import (
    INFRASTRUCTURE_ARGUMENT_NAMES,
    ProvenanceMCP,
)
from docket.mcp.server import mcp
from docket.mcp.triage_server import triage_mcp
from docket.models import AuthenticatedRequest, ChangeSet, PreparedCommand
from docket.plugin.business import BUSINESS_TOOLS, register_business
from docket.plugin.commands import NATIVE_COMMAND_TOOLS, register_commands
from docket.plugin.events import EventMethods, advertise_events
from docket.plugin.instructions import NATIVE_INSTRUCTIONS
from docket.plugin.schemas import compact_schema
from docket.plugin.work import NativeWork
from docket.plugin.workflows import NATIVE_WORKFLOW_TOOLS, register_workflows
from docket.providers.webhooks import WebhookPoster
from docket.schemas.business import PLUGIN_COMPATIBILITY_TOOLS
from docket.services.native_commands import NativeCommandService
from docket.services.notifications import NotificationService
from docket.tool_contracts import contract_tool_names


class PluginMCP(ProvenanceMCP):
    authority_tools = NATIVE_WORKFLOW_TOOLS | NATIVE_COMMAND_TOOLS | BUSINESS_TOOLS

    def __init__(self, settings: Settings, poster: WebhookPoster | None = None) -> None:
        hostname = urlsplit(settings.plugin_public_origin).hostname or "docket.you"
        super().__init__(
            "Docket", caller_profile="interactive", contract_profile="plugin",
            instructions=NATIVE_INSTRUCTIONS,
            extensions=[EventMethods(settings, poster)], middleware=[advertise_events],
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[hostname, hostname + ":443", "127.0.0.1:*", "docket:8000"],
                allowed_origins=[settings.plugin_public_origin.rstrip("/")],
            ),
        )
        self.plugin_settings = settings
        register_commands(self, settings)
        register_business(self, settings)
        register_workflows(self, settings)
        annotations = ToolAnnotations(
            read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        )
        # Cached clients retain their original registered aliases/permission path.
        names = contract_tool_names("plugin") | PLUGIN_COMPATIBILITY_TOOLS
        for info in mcp._tool_manager.list_tools():
            if info.name in names and info.name not in self.authority_tools:
                self.add_tool(info.fn, name=info.name, description=info.description,
                              annotations=ToolAnnotations(
                                  read_only_hint=True,
                                  destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False,
                              ))
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

    def prepare_agent_call(
        self, name: str, authenticated: AgentCallContext, arguments: dict[str, Any],
    ) -> tuple[AgentCallContext, dict[str, Any]]:
        if name == "docket_apply_change":
            with session_scope() as session:
                proposal = session.scalar(select(PreparedCommand).where(
                    PreparedCommand.ref_id == arguments.get("change_id"),
                    PreparedCommand.principal_ref == authenticated.principal.principal_ref,
                ))
                root = session.scalar(select(AuthenticatedRequest).where(
                    AuthenticatedRequest.ref_id == proposal.request_ref,
                )) if proposal is not None else None
                if root is None or proposal is None:
                    raise DocketError(code="change_unavailable",
                                      message="Apply your exact owned change handle.")
                return AgentCallContext(authenticated.principal, root.request_key,
                    "native-exact-proposal", "apply:" + proposal.ref_id,
                    supplied_argument_names=frozenset(arguments)), arguments
        if name in NATIVE_COMMAND_TOOLS | BUSINESS_TOOLS and arguments.get("replaces_change_id"):
            with session_scope() as session:
                proposal = session.scalar(select(PreparedCommand).where(
                    PreparedCommand.ref_id == arguments["replaces_change_id"],
                    PreparedCommand.principal_ref == authenticated.principal.principal_ref,
                ))
                root = session.scalar(select(AuthenticatedRequest).where(
                    AuthenticatedRequest.ref_id == proposal.request_ref,
                )) if proposal is not None else None
                draft = session.scalar(select(ChangeSet).where(
                    ChangeSet.ref_id == proposal.changeset_ref,
                )) if proposal is not None else None
                if proposal is None or root is None or draft is None:
                    raise DocketError(
                        code="change_unavailable", message="Correct your owned preview.",
                    )
                if root.state != "active":
                    raise DocketError(code="change_already_committed",
                                      message="Use an update command for already committed work.")
                if NativeCommandService.fingerprint(draft) != proposal.revision_hash:
                    raise DocketError(code="prepared_change_stale",
                                      message="Correct the exact preview you last observed.")
                return AgentCallContext(authenticated.principal, root.request_key,
                    "native-exact-proposal", "correct:" + str(arguments.get("operation_id")),
                    supplied_argument_names=frozenset(arguments)), arguments
        if name in self.authority_tools:
            operation = arguments.get("operation_id")
            try:
                work = NativeWork.model_validate({"request_id": (
                        "command-" + hashlib.sha256(str(operation).encode()).hexdigest()),
                        "execution_id": "native-command", "operation_id": operation})
            except ValidationError as exc:
                raise DocketError(code="invalid_operation_id",
                                  message="Supply a stable operation_id for this command.",
                                  details={"field": "operation_id"}) from exc
            return replace(work.bind(authenticated.principal),
                           supplied_argument_names=frozenset(arguments)), arguments
        return authenticated, arguments

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        authenticated = agent_call_context.get()
        result = await super().call_tool(name, arguments, context)
        if (
            name in self.authority_tools and authenticated is not None
            and authenticated.principal.oauth_write_eligible
            and "stage" not in authenticated.principal.permissions
            and (result.structured_content or {}).get("error", {}).get("code")
            == "agent_authority_denied"
        ):
            origin = self.plugin_settings.plugin_public_origin.rstrip("/")
            scopes = (self.plugin_settings.plugin_read_scope + " "
                      + self.plugin_settings.plugin_write_scope)
            result.meta = {**(result.meta or {}), "mcp/www_authenticate": [
                f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource", '
                f'scope="{scopes}", error="insufficient_scope", '
                'error_description="Authorize delegated Docket writes to continue"',
            ]}
        return result

    async def list_tools(self) -> list[Tool]:
        tools = [tool for tool in await super().list_tools()
                 if tool.name not in PLUGIN_COMPATIBILITY_TOOLS]
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
            if tool.name in self.authority_tools:
                scopes.append(self.plugin_settings.plugin_write_scope)
            if tool.name in TRIAGE_WORK_TOOLS:
                scopes.append(self.plugin_settings.plugin_triage_scope)
            tool.meta = {
                **(tool.meta or {}),
                "securitySchemes": [{"type": "oauth2", "scopes": scopes}],
            }
            if tool.name == "docket_get_profile":
                tool.meta["openai/profile"] = True
            tool.input_schema = compact_schema(tool.input_schema)
        return tools

"""Thin native work handlers; inference belongs to the connected agent."""

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.work import WORK_TOOL_PURPOSES, GetWorkContext
from docket.services.business_commands import public_validation_error
from docket.services.native_context import NativeContextService
from docket.services.work_commands import WORK_COMMAND_MODELS, WorkCommandService

WORK_COMMAND_TOOLS = frozenset(WORK_COMMAND_MODELS)


def register_work(server: ProvenanceMCP, settings: Settings) -> None:
    def register(name: str) -> None:
        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                if not request_ref or not request_key:
                    raise DocketError(
                        code="request_context_required", message="Use the authenticated connection."
                    )
                with session_scope() as session:
                    return WorkCommandService(session, settings).perform(
                        name,
                        arguments,
                        request_ref,
                        request_key,
                    )
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        server.add_tool(
            model_function(WORK_COMMAND_MODELS[name], name, invoke),
            name=name,
            description=WORK_TOOL_PURPOSES[name],
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )

    for name in WORK_COMMAND_MODELS:
        register(name)

    def context(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetWorkContext.model_validate(arguments)
            with session_scope() as session:
                return NativeContextService(session, settings).work(public.refs, public.search)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(GetWorkContext, "docket_get_work_context", context),
        name="docket_get_work_context",
        description=WORK_TOOL_PURPOSES["docket_get_work_context"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

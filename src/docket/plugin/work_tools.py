"""Thin native work handlers; inference belongs to the connected agent."""

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.compensation import POLICY_TOOL_PURPOSES, GetCompensation
from docket.schemas.work import WORK_TOOL_PURPOSES, GetWorkContext, QueryWorkSessions
from docket.services.business_commands import public_validation_error
from docket.services.compensation_evaluations import CompensationEvaluationService
from docket.services.compensation_policies import (
    POLICY_COMMAND_MODELS,
    CompensationPolicyCommandService,
)
from docket.services.native_context import NativeContextService
from docket.services.work_commands import WORK_COMMAND_MODELS, WorkCommandService
from docket.services.work_lineage import LINEAGE_COMMAND_MODELS, WorkLineageCommandService
from docket.services.work_reports import WorkReportService
from docket.services.work_timers import TIMER_COMMAND_MODELS, WorkTimerCommandService

WORK_COMMAND_TOOLS = (
    frozenset(WORK_COMMAND_MODELS)
    | frozenset(TIMER_COMMAND_MODELS)
    | frozenset(LINEAGE_COMMAND_MODELS)
    | frozenset(POLICY_COMMAND_MODELS)
)


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
                    service = (
                        WorkTimerCommandService
                        if name in TIMER_COMMAND_MODELS
                        else CompensationPolicyCommandService
                        if name in POLICY_COMMAND_MODELS
                        else WorkLineageCommandService
                        if name in LINEAGE_COMMAND_MODELS
                        else WorkCommandService
                    )
                    return service(session, settings).perform(
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
            model_function(
                (
                    {
                        **WORK_COMMAND_MODELS,
                        **TIMER_COMMAND_MODELS,
                        **LINEAGE_COMMAND_MODELS,
                        **POLICY_COMMAND_MODELS,
                    }
                )[name],
                name,
                invoke,
            ),
            name=name,
            description={**WORK_TOOL_PURPOSES, **POLICY_TOOL_PURPOSES}[name],
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )

    for name in (
        *WORK_COMMAND_MODELS,
        *TIMER_COMMAND_MODELS,
        *LINEAGE_COMMAND_MODELS,
        *POLICY_COMMAND_MODELS,
    ):
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

    def query(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = QueryWorkSessions.model_validate(arguments)
            with session_scope() as session:
                return WorkReportService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(QueryWorkSessions, "docket_query_work_sessions", query),
        name="docket_query_work_sessions",
        description=WORK_TOOL_PURPOSES["docket_query_work_sessions"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

    def compensation(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            public = GetCompensation.model_validate(arguments)
            with session_scope() as session:
                return CompensationEvaluationService(session, settings).query(public)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(GetCompensation, "docket_get_compensation", compensation),
        name="docket_get_compensation",
        description=POLICY_TOOL_PURPOSES["docket_get_compensation"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )

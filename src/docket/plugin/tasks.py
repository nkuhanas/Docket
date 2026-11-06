"""Six task workflows with flat public schemas and thin authenticated handlers."""

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.native_tasks import (
    TASK_TOOL_PURPOSES,
    ConfigureTaskList,
    GetTasksContext,
    LinkGoogleTask,
    QueryTasks,
    RefreshTasks,
    ResolveTaskSync,
)
from docket.services.business_commands import public_validation_error
from docket.services.task_commands import TaskCommandService
from docket.services.tasks_context import TasksContextService
from docket.services.tasks_polling import TasksPollingService

TASK_COMMAND_TOOLS = frozenset({
    "docket_configure_task_list", "docket_link_google_task", "docket_resolve_task_sync",
})


def register_tasks(server: ProvenanceMCP, settings: Settings) -> None:
    definitions: dict[str, type[BaseModel]] = {
        "docket_get_tasks_context": GetTasksContext,
        "docket_query_tasks": QueryTasks,
        "docket_configure_task_list": ConfigureTaskList,
        "docket_refresh_tasks": RefreshTasks,
        "docket_link_google_task": LinkGoogleTask,
        "docket_resolve_task_sync": ResolveTaskSync,
    }

    def register(name: str, model: type[BaseModel]) -> None:
        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                data = model.model_validate(arguments)
                with session_scope() as session:
                    if isinstance(data, GetTasksContext):
                        return TasksContextService(session, settings).context(data)
                    if isinstance(data, QueryTasks):
                        return TasksContextService(session, settings).query(data)
                    if isinstance(data, RefreshTasks):
                        return TasksPollingService.request(session, data.task_list_ref)
                    assert isinstance(data, ConfigureTaskList | LinkGoogleTask | ResolveTaskSync)
                    if request_ref is None or request_key is None:
                        raise DocketError(
                            code="request_context_required",
                            message="An authenticated command request is required.",
                        )
                    return TaskCommandService(session, settings).prepare(
                        data, request_ref, request_key,
                    )
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        description = TASK_TOOL_PURPOSES[name]
        if name in TASK_COMMAND_TOOLS:
            description += (
                " Reuse operation_id for unchanged retries; preview=true returns an exact proposal."
            )
        server.add_tool(
            model_function(model, name, invoke), name=name, description=description,
            annotations=ToolAnnotations(
                read_only_hint=name not in TASK_COMMAND_TOOLS,
                destructive_hint=name == "docket_configure_task_list",
                idempotent_hint=True, open_world_hint=name in TASK_COMMAND_TOOLS,
            ),
        )

    for name, model in definitions.items():
        register(name, model)

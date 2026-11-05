"""Thin native business-tool registration; services own transactions and domain rules."""

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.common import PublicRef
from docket.schemas.native_commands import (
    ApplyChange,
    CalendarRef,
    CancelEvent,
    ChangeId,
    CompleteTask,
    CreateEvent,
    CreateTask,
    PrepareBatch,
    UpdateEvent,
    UpdateTask,
)
from docket.services.business_commands import public_validation_error
from docket.services.native_commands import NativeCommandService
from docket.services.native_context import NativeContextService
from docket.services.prepared_batches import PreparedBatchService
from docket.services.routine_commands import RoutineCommandService

ROUTINE_COMMANDS: dict[str, tuple[type[BaseModel], str]] = {
    "docket_create_event": (
        CreateEvent,
        "Create an event from title, ISO start/end, timezone and calendar_ref.",
    ),
    "docket_update_event": (
        UpdateEvent,
        "Update supplied fields; null clears location/notes. "
        "Choose occurrence or series scope for recurring events.",
    ),
    "docket_cancel_event": (
        CancelEvent,
        "Cancel the selected event, original occurrence or entire series.",
    ),
    "docket_create_task": (
        CreateTask,
        "Create a task with title and optional notes/priority/due date. "
        "Docket derives tracking records.",
    ),
    "docket_update_task": (
        UpdateTask,
        "Update supplied task fields. state=completed finishes the task; "
        "null due clears its observed due entry.",
    ),
    "docket_complete_task": (
        CompleteTask,
        "Complete a task; Docket records completion time unless supplied.",
    ),
}
NATIVE_COMMAND_TOOLS = frozenset(ROUTINE_COMMANDS) | {"docket_apply_change", "docket_prepare_batch"}
NATIVE_CONTEXT_TOOLS = frozenset(
    {"docket_get_overview", "docket_get_calendar_context", "docket_get_context"}
)


def register_commands(server: ProvenanceMCP, settings: Settings) -> None:
    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(annotations=read)
    def docket_get_overview() -> dict[str, Any]:
        """Start with timezone, capabilities and short workflow guidance; reuse suitable context."""
        with session_scope() as session:
            return NativeContextService(session, settings).overview()

    @server.tool(annotations=read)
    def docket_get_calendar_context(calendar_ref: CalendarRef | None = None) -> dict[str, Any]:
        """Read calendar choices and bounded canonical observations; does not refresh Google."""
        try:
            with session_scope() as session:
                return NativeContextService(session, settings).calendar(calendar_ref)
        except DocketError as exc:
            return exc.as_dict()

    @server.tool(annotations=read)
    def docket_get_context(
        refs: Annotated[list[PublicRef], Field(min_length=1, max_length=25)],
    ) -> dict[str, Any]:
        """Read selected canonical objects, observed versions and their available actions."""
        try:
            with session_scope() as session:
                return NativeContextService(session, settings).objects(refs)
        except DocketError as exc:
            return exc.as_dict()

    @server.tool(annotations=read)
    def docket_read_change(change_id: ChangeId) -> dict[str, Any]:
        """Read the exact retained preview, state and field issues for an owned change handle."""
        try:
            with session_scope() as session:
                return NativeCommandService(session, settings).read(change_id)
        except DocketError as exc:
            return exc.as_dict()

    def apply(arguments: dict[str, Any]) -> dict[str, Any]:
        arguments.pop("request_ref", None)
        arguments.pop("request_key", None)
        try:
            value = ApplyChange.model_validate(arguments)
            with session_scope() as session:
                return NativeCommandService(session, settings).apply(value.change_id)
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(ApplyChange, "docket_apply_change", apply),
        name="docket_apply_change",
        description="Apply this exact owned prepared proposal; never the latest draft. "
        "No mutation payload is accepted. Retrying returns its original receipt.",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )

    def prepare_batch(arguments: dict[str, Any]) -> dict[str, Any]:
        request_ref = arguments.pop("request_ref", None)
        request_key = arguments.pop("request_key", None)
        try:
            if request_ref is None or request_key is None:
                raise DocketError(
                    code="request_context_required",
                    message="An authenticated batch request is required.",
                )
            value = PrepareBatch.model_validate(arguments)
            with session_scope() as session:
                return PreparedBatchService(session, settings).prepare(
                    value.operation_id,
                    value.change_ids,
                    request_ref,
                    request_key,
                )
        except ValidationError as exc:
            return public_validation_error(exc).as_dict()
        except DocketError as exc:
            return exc.as_dict()

    server.add_tool(
        model_function(PrepareBatch, "docket_prepare_batch", prepare_batch),
        name="docket_prepare_batch",
        description="Combine ready owned previews for different targets into one atomic "
        "change. Preparation does not commit; apply the returned change_id. "
        "A member applied by the batch cannot execute again separately.",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )

    def register(name: str, model: type[BaseModel], description: str) -> None:
        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                if request_ref is None or request_key is None:
                    raise DocketError(
                        code="request_context_required",
                        message="An authenticated command request is required.",
                    )
                command = model.model_validate(arguments)
                with session_scope() as session:
                    if isinstance(command, CreateEvent):
                        return NativeCommandService(session, settings).create_event(
                            command,
                            request_ref,
                            request_key,
                            prepare=command.preview,
                        )
                    service = RoutineCommandService(session, settings)
                    if isinstance(command, UpdateEvent | CancelEvent):
                        return service.event(command, request_ref, request_key)
                    assert isinstance(command, CreateTask | UpdateTask | CompleteTask)
                    return service.task(command, request_ref, request_key)
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        server.add_tool(
            model_function(model, name, invoke),
            name=name,
            description=description + " Reuse operation_id for unchanged retries. "
            "Set preview=true only when requested; apply the exact returned change_id.",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=name == "docket_cancel_event",
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )

    for name, (model, description) in ROUTINE_COMMANDS.items():
        register(name, model, description)

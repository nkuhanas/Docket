"""Thin business-tool registration; services retain domain and transaction rules."""

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.schemas.native_commands import CalendarRef, ContextId, CreateEvent, OperationId
from docket.services.native_commands import NativeCommandService
from docket.services.native_context import NativeContextService

NATIVE_COMMAND_TOOLS = frozenset({"docket_create_event"})
NATIVE_CONTEXT_TOOLS = frozenset({"docket_get_overview", "docket_get_calendar_context"})


def register_commands(server: ProvenanceMCP, settings: Settings) -> None:
    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
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

    @server.tool(annotations=write)
    def docket_create_event(
        context_id: ContextId,
        operation_id: OperationId,
        title: Annotated[str, Field(min_length=1, max_length=512)],
        start: Annotated[str, Field(max_length=64)],
        end: Annotated[str, Field(max_length=64)],
        timezone: Annotated[str, Field(max_length=128)],
        calendar_ref: CalendarRef,
        location: Annotated[str | None, Field(max_length=1000)] = None,
        notes: Annotated[str | None, Field(max_length=4000)] = None,
        request_ref: Annotated[
            str | None,
            Field(
                json_schema_extra={"x-docket-internal": True},
            ),
        ] = None,
        request_key: Annotated[
            str | None,
            Field(
                json_schema_extra={"x-docket-internal": True},
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create one event and its tracking records; Google delivery follows canonical commit.

        Read calendar context, choose its calendar_ref and supply ISO start/end with
        timezone, e.g. 2026-11-13T13:00:00-08:00 / America/Los_Angeles.
        Reuse operation_id and unchanged arguments for retries. Notes do not send invitations.
        """
        try:
            if request_ref is None or request_key is None:
                raise DocketError(
                    code="request_context_required",
                    message="An authenticated command request is required.",
                )
            command = CreateEvent(
                context_id=context_id,
                operation_id=operation_id,
                title=title,
                start=start,
                end=end,
                timezone=timezone,
                calendar_ref=calendar_ref,
                location=location,
                notes=notes,
            )
            with session_scope() as session:
                return NativeCommandService(session, settings).create_event(
                    command,
                    request_ref,
                    request_key,
                )
        except DocketError as exc:
            return exc.as_dict()

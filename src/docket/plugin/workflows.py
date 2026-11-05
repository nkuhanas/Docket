"""Small native clarification/conflict/capture and bounded recovery tools."""

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.plugin.business import model_function
from docket.schemas.common import PublicRef
from docket.schemas.native_workflows import (
    RecordContext,
    RequestClarification,
    ResolveAttentionCase,
    ResolveConflict,
)
from docket.services.business_commands import public_validation_error
from docket.services.native_recovery import NativeRecoveryService
from docket.services.native_workflows import NativeWorkflowService

WORKFLOW_MODELS: dict[
    str,
    type[RecordContext]
    | type[RequestClarification]
    | type[ResolveConflict]
    | type[ResolveAttentionCase],
] = {
    "docket_record_context": RecordContext,
    "docket_request_clarification": RequestClarification,
    "docket_resolve_conflict": ResolveConflict,
    "docket_resolve_attention_case": ResolveAttentionCase,
}
NATIVE_WORKFLOW_TOOLS = frozenset(WORKFLOW_MODELS)


def register_workflows(server: ProvenanceMCP, settings: Settings) -> None:
    def register(name: str) -> None:
        model = WORKFLOW_MODELS[name]

        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                if request_ref is None or request_key is None:
                    raise DocketError(
                        code="request_context_required", message="Use the authenticated connection."
                    )
                value = model.model_validate(arguments)
                with session_scope() as session:
                    return NativeWorkflowService(session, settings).perform(
                        value,
                        request_ref,
                        request_key,
                    )
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        server.add_tool(
            model_function(model, name, invoke),
            name=name,
            description={
                "docket_resolve_attention_case": "Record dispositions for selected attention items "
                "and a case outcome using context-held revision/version. Required unresolved items "
                "keep the case open; unrelated work and optional capture remain independent.",
                "docket_record_context": "Optionally archive agent-reported text or a capture gap "
                "in a separate transaction. Missing capture never blocks work. "
                "Append corrections with a new operation_id; relayed text remains agent-reported.",
                "docket_request_clarification": "Retain a genuine unresolved question and bounded "
                "choices. Ask the user, then use a named command for answered intent.",
                "docket_resolve_conflict": "Resolve a selected conflict using context-held version "
                "and its statement references. Optionally include one exact owned preview's "
                "effects in the same atomic transaction. Reuse operation_id for unchanged retries.",
            }[name],
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=name == "docket_resolve_conflict",
                idempotent_hint=True,
                open_world_hint=name == "docket_resolve_conflict",
            ),
        )

    for name in WORKFLOW_MODELS:
        register(name)

    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(annotations=read)
    def docket_list_saved_work(
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: PublicRef | None = None,
    ) -> dict[str, Any]:
        """List owned active saved requests, including pre-cutover drafts. No execution/deletion."""
        try:
            with session_scope() as session:
                return NativeRecoveryService(session).list(limit, cursor)
        except DocketError as exc:
            return exc.as_dict()

    @server.tool(annotations=read)
    def docket_read_saved_work(
        saved_request_ref: Annotated[str, Field(pattern=r"^req_[0-9A-HJKMNP-TV-Z]{26}$")],
        revision: Annotated[int | None, Field(ge=1)] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Recover owned metadata and exact saved revision JSON in bounded chunks."""
        try:
            with session_scope() as session:
                return NativeRecoveryService(session).read(saved_request_ref, revision, offset)
        except DocketError as exc:
            return exc.as_dict()

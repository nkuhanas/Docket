"""Register exact, small named schemas from public business models."""

import inspect
from collections.abc import Callable
from typing import Annotated, Any, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from docket.agent_auth import agent_call_context
from docket.config import Settings
from docket.database import session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceMCP
from docket.schemas.business import BUSINESS_DEFINITIONS, BusinessDefinition
from docket.services.business_commands import BusinessCommandService, public_validation_error

BUSINESS_TOOLS = frozenset(definition.tool_name for definition in BUSINESS_DEFINITIONS)


def model_function(
    model: type[BaseModel], name: str, callback: Callable[[dict[str, Any]], dict[str, Any]]
) -> Callable[..., Any]:
    def business_command(**arguments: Any) -> dict[str, Any]:
        context = agent_call_context.get()
        supplied = context.supplied_argument_names if context else None
        if supplied is not None:
            arguments = {
                key: value
                for key, value in arguments.items()
                if key in supplied or key in {"request_ref", "request_key"}
            }
        return callback(arguments)

    parameters = [
        inspect.Parameter(
            field_name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[field.annotation, field],
            default=inspect.Parameter.empty if field.is_required() else field,
        )
        for field_name, field in model.model_fields.items()
    ]
    hidden = Annotated[str | None, Field(json_schema_extra={"x-docket-internal": True})]
    for field_name in ("request_ref", "request_key"):
        parameters.append(
            inspect.Parameter(
                field_name, inspect.Parameter.KEYWORD_ONLY, annotation=hidden, default=None
            )
        )
    cast(Any, business_command).__signature__ = inspect.Signature(
        parameters, return_annotation=dict[str, Any]
    )
    business_command.__name__ = name
    return business_command


def register_business(server: ProvenanceMCP, settings: Settings) -> None:
    def register(definition: BusinessDefinition) -> None:
        def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            request_ref = arguments.pop("request_ref", None)
            request_key = arguments.pop("request_key", None)
            try:
                if request_ref is None or request_key is None:
                    raise DocketError(
                        code="request_context_required",
                        message="An authenticated command request is required.",
                    )
                with session_scope() as session:
                    return BusinessCommandService(session, settings).perform(
                        definition,
                        arguments,
                        request_ref,
                        request_key,
                    )
            except ValidationError as exc:
                return public_validation_error(exc).as_dict()
            except DocketError as exc:
                return exc.as_dict()

        fn = model_function(definition.input_model, definition.tool_name, invoke)
        server.add_tool(
            fn,
            name=definition.tool_name,
            description=f"{definition.action.capitalize()} {definition.object_type} "
            "using business fields and context-held versions. "
            "Set preview=true only when a preview is wanted; apply its exact change_id. "
            "Reuse operation_id and unchanged arguments for retries.",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=definition.action in {"retract", "unbind", "supersede"},
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )

    for definition in BUSINESS_DEFINITIONS:
        register(definition)

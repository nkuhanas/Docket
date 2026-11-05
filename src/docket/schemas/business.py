"""Named business inputs compiled from domain models, never the mutation union."""

from copy import deepcopy
from dataclasses import dataclass
from functools import reduce
from operator import or_
from types import GenericAlias, UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, Field, create_model
from pydantic_core import PydanticUndefined

from docket.schemas import authority
from docket.schemas.common import StrictModel
from docket.schemas.native_commands import CommandBase


class BusinessCommand(CommandBase):
    pass


@dataclass(frozen=True)
class BusinessDefinition:
    mutation: type[BaseModel]
    input_model: type[BaseModel]
    tool_name: str
    object_type: str
    action: str
    spec_type: type[BaseModel]
    spec_field: str
    replacement: bool = False


def _models(value: Any) -> list[type[BaseModel]]:
    if hasattr(value, "__value__"):
        return _models(value.__value__)
    if isinstance(value, type) and issubclass(value, BaseModel):
        return [value]
    arguments = get_args(value)
    if get_origin(value) is Annotated:
        return _models(arguments[0])
    return [model for argument in arguments for model in _models(argument)]


_REQUIRED_REFS = {
    "AffiliationCreateSpec": {"subject_ref", "organization_ref"},
    "RelationshipCreateSpec": {"subject_ref", "object_ref"},
    "FactCreateSpec": {"subject_ref"},
    "InteractionParticipantInput": {"entity_ref"},
    "TemporalBindingInput": {"subject_ref"},
    "TemporalCalendarProjectionInput": {"temporal_binding_ref", "lane_ref"},
    "ReminderPlanInput": {"subject_ref"},
    "LaneRoutingDecisionCreateSpec": {"lane_ref"},
}
_PUBLIC_MODELS: dict[type[BaseModel], type[BaseModel]] = {}


def _nonnull(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return Annotated[_nonnull(args[0]), *args[1:]]
    if get_origin(annotation) in {Union, UnionType}:
        return reduce(or_, (arg for arg in get_args(annotation) if arg is not type(None)))
    return annotation


def _public_type(annotation: Any) -> Any:
    if hasattr(annotation, "__value__"):
        return _public_type(annotation.__value__)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _public_model(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return Annotated[_public_type(args[0]), *args[1:]]
    if origin is list:
        return GenericAlias(list, _public_type(args[0]))
    if origin in {Union, UnionType}:
        return reduce(or_, (_public_type(arg) for arg in args))
    if origin is dict:
        return GenericAlias(dict, (args[0], _public_type(args[1])))
    return annotation


def _public_model(model: type[BaseModel]) -> type[BaseModel]:
    if model not in _PUBLIC_MODELS:
        fields: dict[str, Any] = {}
        for name, field in model.model_fields.items():
            if name.endswith(("_change_id", "_change_ids")) or name in {"ref_id", "canonical_key"}:
                continue
            public = deepcopy(field)
            annotation = _public_type(field.rebuild_annotation())
            if name in _REQUIRED_REFS.get(model.__name__, set()):
                annotation = _nonnull(annotation)
                public.default = PydanticUndefined
                public.default_factory = None
            fields[name] = (annotation, public)
        _PUBLIC_MODELS[model] = create_model(
            "Public" + model.__name__, __base__=StrictModel, **fields
        )
    return _PUBLIC_MODELS[model]


def _business_definition(mutation: type[BaseModel]) -> BusinessDefinition:
    fields = mutation.model_fields
    object_type = get_args(fields["object_type"].annotation)[0]
    action = get_args(fields["action"].annotation)[0]
    verb = {
        "create": "create",
        "modify": "update",
        "update": "update",
        "retract": "retract",
        "supersede": "replace",
        "bind": "bind",
        "unbind": "unbind",
        "cancel": "cancel",
    }[fields["mutation_type"].default.split("_")[-1]]
    spec_field = "create_spec" if fields["create_spec"].annotation is not type(None) else "payload"
    spec_annotation = fields[spec_field].annotation
    replacement = get_origin(spec_annotation) is dict
    spec_type = get_args(spec_annotation)[1] if replacement else spec_annotation
    if not isinstance(spec_type, type) or not issubclass(spec_type, BaseModel):
        raise TypeError(f"A named domain model is required for {mutation.__name__}")
    public_fields: dict[str, Any] = {}
    if len(get_args(fields["action"].annotation)) > 1:
        public_fields["replace"] = (
            bool,
            Field(
                default=False,
                description="Use replacement semantics instead of an ordinary update.",
            ),
        )
    # Targets have context-held versions; callers never send versions or internal create IDs.
    if fields["object_ref"].annotation is not type(None):
        annotation = _nonnull(fields["object_ref"].rebuild_annotation())
        public_fields["target_ref"] = (annotation, ...)
    for name, field in _public_model(spec_type).model_fields.items():
        if name.endswith(("_change_id", "_change_ids")) or name in {"ref_id", "canonical_key"}:
            continue
        public_fields[name] = (field.rebuild_annotation(), deepcopy(field))
    model = create_model(mutation.__name__ + "Command", __base__=BusinessCommand, **public_fields)
    return BusinessDefinition(
        mutation,
        model,
        f"docket_{verb}_{object_type}",
        object_type,
        action,
        spec_type,
        spec_field,
        replacement,
    )


CANONICAL_MUTATIONS = tuple(_models(authority.CanonicalChangeInput))
# Calendar and tasks get purpose-built everyday commands with derived support records.
SPECIAL_TOOL_BY_MUTATION = {
    "canonical_event_create": "docket_create_event",
    "canonical_event_modify": "docket_update_event",
    "canonical_event_cancel": "docket_cancel_event",
    "task_create": "docket_create_task",
    "task_modify": "docket_update_task",
    "engagement_create": "docket_create_engagement",
    "engagement_modify": "docket_update_engagement",
    "work_session_create": "docket_log_work_session",
    "work_session_modify": "docket_update_work_session",
}
SPECIAL_MUTATIONS = frozenset(SPECIAL_TOOL_BY_MUTATION)
BUSINESS_DEFINITIONS = tuple(
    _business_definition(model)
    for model in CANONICAL_MUTATIONS
    if model.model_fields["mutation_type"].default not in SPECIAL_MUTATIONS
)
BUSINESS_BY_TOOL = {definition.tool_name: definition for definition in BUSINESS_DEFINITIONS}

# This closed set dispatches one logical retraction to existing domain handlers.
# Identity unbinding also uses idn_ but is a different operation and is excluded.
RETRACTION_BY_PREFIX = {
    prefix: BUSINESS_BY_TOOL["docket_retract_" + kind]
    for prefix, kind in (
        ("ent", "entity"), ("idn", "identity_handle"), ("aff", "affiliation"),
        ("rel", "relationship"), ("fact", "fact"), ("pref", "preference"),
        ("lane", "calendar_lane"), ("item", "item"), ("time", "temporal_binding"),
        ("task", "task"), ("tproj", "temporal_calendar_projection"), ("rem", "reminder_plan"),
    )
}
RETRACTION_ALIASES = frozenset(value.tool_name for value in RETRACTION_BY_PREFIX.values())
PLUGIN_COMPATIBILITY_TOOLS = RETRACTION_ALIASES | {
    "docket_complete_task", "docket_get_conflict",
}


class RetractRecord(BusinessCommand):
    target_ref: Annotated[str, Field(
        pattern="^(?:" + "|".join(sorted(RETRACTION_BY_PREFIX)) + ")_[0-9A-HJKMNP-TV-Z]{26}$",
    )]


def public_business_tool(definition: BusinessDefinition) -> str:
    return (
        "docket_retract_record"
        if definition.tool_name in RETRACTION_ALIASES else definition.tool_name
    )


def public_effect_tools() -> dict[str, str]:
    return {
        **{
            definition.mutation.model_fields["mutation_type"].default: (
                public_business_tool(definition)
            )
            for definition in BUSINESS_DEFINITIONS
        },
        **SPECIAL_TOOL_BY_MUTATION,
    }

"""Translate named business fields to guarded canonical plans."""

from typing import Any

from pydantic import ValidationError

from docket.domain.errors import DocketError
from docket.domain.public_refs import is_public_ref
from docket.models import NativeContext
from docket.schemas.authority import mutation_input_json
from docket.schemas.business import BusinessDefinition
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.native_commands import NativeCommandService

_STATUS_FIELDS = {
    "entity": "canonical_status",
    "identity_handle": "status",
    "identity_binding": "entity_ref",
    "affiliation": "status",
    "relationship": "status",
    "fact": "status",
    "preference": "status",
    "calendar_lane": "status",
    "item": "canonical_status",
    "temporal_binding": "canonical_status",
    "task": "canonical_status",
    "temporal_calendar_projection": "enabled",
    "reminder_plan": "canonical_status",
}


def public_validation_error(error: ValidationError) -> DocketError:
    issues = []
    for issue in error.errors(include_url=False, include_input=False):
        location = [
            "target_ref" if part == "object_ref" else part
            for part in issue["loc"]
            if part not in {"payload", "create_spec", "replacement"}
        ]
        issues.append(
            {
                "field": ".".join(str(part) for part in location) or "fields",
                "message": issue["msg"],
                "expected": issue["type"],
            }
        )
    return DocketError(
        code="invalid_business_fields",
        message="Correct the indicated business fields and retry.",
        details={"issues": issues[:20]},
    )


def _named_refs(value: Any, field_name: str = "") -> set[str]:
    if field_name in {"source_refs", "basis_refs"}:
        return set()
    if isinstance(value, str):
        return {value} if field_name.endswith(("_ref", "_refs")) and is_public_ref(value) else set()
    if isinstance(value, dict):
        return {
            ref
            for key, child in value.items()
            if key != "metadata_json"
            for ref in _named_refs(child, key)
        }
    if isinstance(value, list):
        return {ref for child in value for ref in _named_refs(child, field_name)}
    return set()


def context_targets(context: NativeContext) -> set[str]:
    targets = set(context.versions)
    for key in ("objects", "accounts", "calendars", "canonical_events"):
        for obj in context.snapshot.get(key, []):
            for name in ("ref", "account_ref", "calendar_ref", "event_ref"):
                if isinstance(obj.get(name), str):
                    targets.add(obj[name])
    return targets


def mutation_plan(
    context: NativeContext, actions: list[dict[str, Any]], command: str
) -> dict[str, Any]:
    substantive = {ref for action in actions for ref in _named_refs(action)}
    missing = substantive - context_targets(context)
    if missing:
        raise DocketError(
            code="targets_not_in_context",
            message="Read context for these targets before editing or linking them.",
            details={
                "field": "context_id",
                "refs": sorted(missing)[:25],
                "next_action": "docket_get_context",
            },
        )
    # The existing compiler also recognizes opaque public-ref-shaped strings.
    # Admitting them as correlation never grants permission or invents a target version.
    targets = {
        ref for action in actions for ref in ChangeSetAssemblyService._semantic_target_refs(action)
    }
    return {
        "expected_versions": {
            ref: version for ref, version in context.versions.items() if ref in substantive
        },
        "assembly_scope": {
            "resolved_intent": {"business_command": command},
            "allowed_mutation_types": sorted({action["mutation_type"] for action in actions}),
            "target_refs": sorted(targets),
            "planned_create_types": sorted(
                {
                    action["object_type"]
                    for action in actions
                    if action["action"] in {"create", "supersede"}
                }
            ),
            "event_scopes": {
                action["object_ref"]: action["scope"]
                for action in actions
                if "scope" in action and action.get("object_ref")
            },
        },
        "patch": {
            "operations": [{"operation": "action_upsert", "action": action} for action in actions]
        },
    }


class BusinessCommandService(NativeCommandService):
    def perform(
        self,
        definition: BusinessDefinition,
        arguments: dict[str, Any],
        request_ref: str,
        request_key: str,
    ) -> dict[str, Any]:
        public = definition.input_model.model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)

        def build(context: NativeContext) -> dict[str, Any]:
            fields = {
                name: values[name] for name in definition.spec_type.model_fields if name in values
            }
            try:
                spec = definition.spec_type.model_validate(fields)
                content = spec.model_dump(mode="json", exclude_unset=True)
                changes = list(content) or [_STATUS_FIELDS.get(definition.object_type, "status")]
                data = {
                    "change_id": "command",
                    "mutation_type": definition.mutation.model_fields["mutation_type"].default,
                    "action": "supersede" if values.get("replace") else definition.action,
                    "object_type": definition.object_type,
                    "affected_fields": changes,
                }
                if definition.mutation.model_fields["object_ref"].annotation is not type(None):
                    data["object_ref"] = values["target_ref"]
                data[definition.spec_field] = (
                    {"replacement": content} if (definition.replacement) else content
                )
                mutation = definition.mutation.model_validate(data)
            except ValidationError as exc:
                raise public_validation_error(exc) from exc
            return mutation_plan(
                context,
                [mutation_input_json(mutation, preserve_explicit_nulls=True)],
                definition.tool_name,
            )

        return self.execute(
            command=definition.tool_name,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

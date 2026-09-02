"""Bounded progressive disclosure for Docket's large ChangeSet schema.

Hermes keeps deferred MCP schemas in its process-local tool registry.  Docket's
complete ChangeSet schema is intentionally broad, but returning the whole schema
from ``tool_describe`` is too large for one model turn.  This module derives an
exact, closed JSON Schema for only the mutation variants needed by the current
semantic request and installs that behavior into the pinned Hermes bridge.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

COMMIT_TOOL_NAME = "docket_commit_changeset"
MUTATION_TYPES_ARGUMENT = "mutation_types"
MAX_MUTATION_TYPES = 16
MAX_SCOPED_DESCRIPTION_BYTES = 48 * 1024

_DEFINITION_REF = re.compile(r"^#/\$defs/([^/]+)$")
_CHANGE_UNIONS = {
    "registry_changes": "RegistryChangeInput",
    "preference_changes": "PreferenceChangeInput",
    "lane_changes": "LaneChangeInput",
    "event_changes": "EventChangeInput",
    "tracked_context_changes": "TrackedContextChangeInput",
}
_DIRECT_MUTATIONS = {
    "attention_case_resolution": ("resolution_changes", "ResolutionChangeInput"),
}


class SchemaScopeError(ValueError):
    """The requested model-facing schema scope is invalid or too broad."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _definition_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            match = _DEFINITION_REF.fullmatch(reference)
            if match is not None:
                yield match.group(1)
        for nested in value.values():
            yield from _definition_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _definition_refs(nested)


def mutation_type_catalog(parameters: dict[str, Any]) -> tuple[str, ...]:
    """Return every exact discriminated mutation type in a commit schema."""
    definitions = parameters.get("$defs")
    if not isinstance(definitions, dict):
        raise SchemaScopeError("commit schema has no $defs object")
    names = set(_DIRECT_MUTATIONS)
    for union_name in _CHANGE_UNIONS.values():
        union = definitions.get(union_name)
        if not isinstance(union, dict):
            raise SchemaScopeError(f"commit schema is missing {union_name}")
        discriminator = union.get("discriminator")
        mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
        if not isinstance(mapping, dict) or not mapping:
            raise SchemaScopeError(f"{union_name} has no discriminator mapping")
        names.update(str(name) for name in mapping)
    return tuple(sorted(names))


def _narrow_union(union: dict[str, Any], selected: dict[str, str]) -> dict[str, Any]:
    narrowed = copy.deepcopy(union)
    discriminator = narrowed["discriminator"]
    discriminator["mapping"] = selected
    branch_key = "oneOf" if "oneOf" in narrowed else "anyOf"
    narrowed[branch_key] = [{"$ref": reference} for reference in selected.values()]
    return narrowed


def scoped_commit_schema(
    parameters: dict[str, Any], mutation_types: Iterable[str]
) -> dict[str, Any]:
    """Build a reference-closed schema for exactly ``mutation_types``.

    Unselected ChangeSet groups are removed from the model-facing view.  They
    retain their normal server-side defaults, so arguments constructed from the
    view still validate against the complete Pydantic schema.
    """
    requested = tuple(dict.fromkeys(str(name).strip() for name in mutation_types if name))
    if not requested:
        raise SchemaScopeError("mutation_types is required for docket_commit_changeset")
    if len(requested) > MAX_MUTATION_TYPES:
        raise SchemaScopeError(
            f"mutation_types accepts at most {MAX_MUTATION_TYPES} exact variants"
        )

    available = set(mutation_type_catalog(parameters))
    unknown = sorted(set(requested) - available)
    if unknown:
        raise SchemaScopeError(f"unknown mutation types: {', '.join(unknown)}")

    scoped = copy.deepcopy(parameters)
    definitions = scoped["$defs"]
    content = definitions.get("OperatorChangeSetContent")
    if not isinstance(content, dict) or not isinstance(content.get("properties"), dict):
        raise SchemaScopeError("commit schema is missing OperatorChangeSetContent")
    content_properties = content["properties"]

    for field_name, union_name in _CHANGE_UNIONS.items():
        union = definitions[union_name]
        mapping = union["discriminator"]["mapping"]
        selected = {
            name: reference for name, reference in mapping.items() if name in requested
        }
        if not selected:
            content_properties.pop(field_name, None)
            continue
        definitions[union_name] = _narrow_union(union, selected)

    for mutation_type, (field_name, _definition_name) in _DIRECT_MUTATIONS.items():
        if mutation_type not in requested:
            content_properties.pop(field_name, None)

    roots = {key: value for key, value in scoped.items() if key != "$defs"}
    needed = set(_definition_refs(roots))
    pending = list(needed)
    while pending:
        definition_name = pending.pop()
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            raise SchemaScopeError(f"schema references missing definition {definition_name}")
        for dependency in _definition_refs(definition):
            if dependency not in needed:
                needed.add(dependency)
                pending.append(dependency)
    scoped["$defs"] = {
        name: definition for name, definition in definitions.items() if name in needed
    }
    return scoped


def scoped_tool_description(
    tool_definition: dict[str, Any], mutation_types: Iterable[str]
) -> dict[str, Any]:
    """Return a bounded, hash-bound description for one ChangeSet scope."""
    function = tool_definition.get("function")
    if not isinstance(function, dict):
        raise SchemaScopeError("tool definition has no function object")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise SchemaScopeError("tool definition has no parameter schema")
    requested = tuple(dict.fromkeys(str(name).strip() for name in mutation_types if name))
    scoped = scoped_commit_schema(parameters, requested)
    full_hash = hashlib.sha256(_canonical_json(parameters).encode()).hexdigest()
    scoped_hash = hashlib.sha256(_canonical_json(scoped).encode()).hexdigest()
    payload = {
        "name": COMMIT_TOOL_NAME,
        "description": function.get("description", ""),
        "schema_scope": {
            "mutation_types": list(requested),
            "complete_for_selected_mutations": True,
            "full_schema_sha256": full_hash,
            "scoped_schema_sha256": scoped_hash,
        },
        "parameters": scoped,
    }
    encoded = _canonical_json(payload).encode()
    if len(encoded) > MAX_SCOPED_DESCRIPTION_BYTES:
        raise SchemaScopeError(
            "requested mutation schema exceeds the bounded disclosure budget; "
            "request fewer exact mutation types"
        )
    return payload


def _find_tool_definition(
    current_tool_defs: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    for definition in current_tool_defs:
        function = definition.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return definition
    return None


def install_hermes_progressive_schema_patch() -> bool:
    """Install the scoped describe extension into pinned Hermes.

    The patch is deliberately narrow: every non-Docket tool and every normal
    ``tool_describe`` call retains upstream behavior.  Returning ``False`` means
    this process is not the Hermes runtime (for example, a repository unit test).
    """
    try:
        from tools import tool_search
    except ImportError:
        return False
    if getattr(tool_search, "_docket_scoped_describe_installed", False):
        return True

    original_dispatch = tool_search.dispatch_tool_describe
    original_bridge_schemas = tool_search.bridge_tool_schemas

    def dispatch_tool_describe(
        args: dict[str, Any], *, current_tool_defs: list[dict[str, Any]]
    ) -> str:
        name = str(args.get("name") or "").strip()
        if name != COMMIT_TOOL_NAME:
            return original_dispatch(args, current_tool_defs=current_tool_defs)
        mutation_types = args.get(MUTATION_TYPES_ARGUMENT)
        if not isinstance(mutation_types, list) or not mutation_types:
            definition = _find_tool_definition(current_tool_defs, name)
            if definition is None:
                return original_dispatch(args, current_tool_defs=current_tool_defs)
            parameters = definition.get("function", {}).get("parameters", {})
            try:
                available = mutation_type_catalog(parameters)
            except SchemaScopeError as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)
            return json.dumps(
                {
                    "error": (
                        "mutation_types is required when describing "
                        "docket_commit_changeset; request only the exact variants "
                        "needed by this semantic request"
                    ),
                    "available_mutation_types": list(available),
                },
                ensure_ascii=False,
            )
        definition = _find_tool_definition(current_tool_defs, name)
        if definition is None:
            return original_dispatch(args, current_tool_defs=current_tool_defs)
        try:
            payload = scoped_tool_description(definition, mutation_types)
        except SchemaScopeError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def bridge_tool_schemas(deferred_count: int) -> list[dict[str, Any]]:
        schemas = original_bridge_schemas(deferred_count)
        for schema in schemas:
            function = schema.get("function")
            if not isinstance(function, dict) or function.get("name") != "tool_describe":
                continue
            parameters = function.get("parameters")
            if not isinstance(parameters, dict):
                raise RuntimeError("Hermes tool_describe schema is incompatible")
            properties = parameters.get("properties")
            if not isinstance(properties, dict) or "name" not in properties:
                raise RuntimeError("Hermes tool_describe schema is incompatible")
            properties[MUTATION_TYPES_ARGUMENT] = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_MUTATION_TYPES,
                "description": (
                    "Required only for docket_commit_changeset. Exact discriminated "
                    "mutation_type values needed by this semantic request; returns a "
                    "complete reference-closed schema containing only those variants."
                ),
            }
            function["description"] = (
                str(function.get("description") or "")
                + " For docket_commit_changeset, mutation_types is required and bounds "
                "the returned exact schema."
            )
        return schemas

    tool_search.dispatch_tool_describe = dispatch_tool_describe
    tool_search.bridge_tool_schemas = bridge_tool_schemas
    tool_search._docket_scoped_describe_installed = True
    return True

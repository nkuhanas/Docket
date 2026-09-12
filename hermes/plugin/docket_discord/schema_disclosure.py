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
STAGE_TOOL_NAME = "docket_stage_changes"
CLARIFICATION_TOOL_NAME = "docket_request_clarification"
NAMESPACED_COMMIT_TOOL_NAME = f"mcp__docket__{COMMIT_TOOL_NAME}"
NAMESPACED_STAGE_TOOL_NAME = f"mcp__docket__{STAGE_TOOL_NAME}"
MUTATION_TYPES_ARGUMENT = "mutation_types"
NORMALIZED_ENTRY_TYPES_ARGUMENT = "normalized_entry_types"
MAX_MUTATION_TYPES = 16
MAX_NORMALIZED_ENTRY_TYPES = 3
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


def _is_commit_tool_name(name: str) -> bool:
    """Accept the public name and the exact Hermes MCP registry name."""
    return name in {COMMIT_TOOL_NAME, NAMESPACED_COMMIT_TOOL_NAME}


def _is_stage_tool_name(name: str) -> bool:
    return name in {STAGE_TOOL_NAME, NAMESPACED_STAGE_TOOL_NAME}


def _is_clarification_tool_name(name: str) -> bool:
    return name in {CLARIFICATION_TOOL_NAME, f"mcp__docket__{CLARIFICATION_TOOL_NAME}"}


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


def _strip_internal_properties(value: Any) -> None:
    """Remove gateway-supplied fields from every model-facing schema object."""
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            hidden = [
                name
                for name, schema in properties.items()
                if isinstance(schema, dict) and schema.get("x-docket-internal") is True
            ]
            for name in hidden:
                properties.pop(name, None)
            required = value.get("required")
            if isinstance(required, list):
                value["required"] = [name for name in required if name not in hidden]
        for nested in value.values():
            _strip_internal_properties(nested)
    elif isinstance(value, list):
        for nested in value:
            _strip_internal_properties(nested)


def _reference_closed(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    roots = {key: value for key, value in schema.items() if key != "$defs"}
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
    schema["$defs"] = {
        name: definition for name, definition in definitions.items() if name in needed
    }
    return schema


def mutation_type_catalog(parameters: dict[str, Any]) -> tuple[str, ...]:
    """Return every exact discriminated mutation type in a commit schema."""
    definitions = parameters.get("$defs")
    if not isinstance(definitions, dict):
        raise SchemaScopeError("commit schema has no $defs object")
    canonical = definitions.get("CanonicalChangeInput")
    if isinstance(canonical, dict):
        discriminator = canonical.get("discriminator")
        mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
        if not isinstance(mapping, dict) or not mapping:
            raise SchemaScopeError("CanonicalChangeInput has no discriminator mapping")
        return tuple(sorted(str(name) for name in mapping))
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
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Commit has no semantic payload; infrastructure owns all its bindings."""
    scoped = copy.deepcopy(parameters)
    _strip_internal_properties(scoped)
    scoped["additionalProperties"] = False
    return _reference_closed(scoped)


def scoped_clarification_schema(
    parameters: dict[str, Any], mutation_types: Iterable[str],
) -> dict[str, Any]:
    """Choices carry exact future effects, but this tool cannot execute them."""
    requested = tuple(dict.fromkeys(str(name).strip() for name in mutation_types if name))
    if not requested:
        raise SchemaScopeError("mutation_types is required for typed clarification choices")
    if len(requested) > MAX_MUTATION_TYPES:
        raise SchemaScopeError(
            f"mutation_types accepts at most {MAX_MUTATION_TYPES} exact variants"
        )

    scoped = copy.deepcopy(parameters)
    _strip_internal_properties(scoped)
    definitions = scoped["$defs"]
    available = set(mutation_type_catalog(parameters))
    unknown = sorted(set(requested) - available)
    if unknown:
        raise SchemaScopeError(f"unknown mutation types: {', '.join(unknown)}")
    content = definitions.get("OperatorChangeSetContent")
    if not isinstance(content, dict) or not isinstance(content.get("properties"), dict):
        raise SchemaScopeError("commit schema is missing OperatorChangeSetContent")
    content_properties = content["properties"]

    for field_name, union_name in _CHANGE_UNIONS.items():
        union = definitions[union_name]
        mapping = union["discriminator"]["mapping"]
        selected = {name: reference for name, reference in mapping.items() if name in requested}
        if not selected:
            content_properties.pop(field_name, None)
            continue
        definitions[union_name] = _narrow_union(union, selected)

    for mutation_type, (field_name, _definition_name) in _DIRECT_MUTATIONS.items():
        if mutation_type not in requested:
            content_properties.pop(field_name, None)

    return _reference_closed(scoped)


def normalized_entry_type_catalog(parameters: dict[str, Any]) -> tuple[str, ...]:
    definitions = parameters.get("$defs", {})
    upsert = definitions.get("StageNormalizedEntryUpsert")
    entry = upsert.get("properties", {}).get("entry") if isinstance(upsert, dict) else None
    discriminator = entry.get("discriminator") if isinstance(entry, dict) else None
    mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
    if not isinstance(mapping, dict) or not mapping:
        raise SchemaScopeError("stage schema has no normalized-entry discriminator")
    return tuple(sorted(str(name) for name in mapping))


def scoped_stage_schema(
    parameters: dict[str, Any],
    *,
    mutation_types: Iterable[str] = (),
    normalized_entry_types: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a bounded staging schema for only the requested patch forms."""
    requested_mutations = tuple(
        dict.fromkeys(str(name).strip() for name in mutation_types if name)
    )
    requested_entries = tuple(
        dict.fromkeys(str(name).strip() for name in normalized_entry_types if name)
    )
    if not requested_mutations and not requested_entries:
        raise SchemaScopeError(
            "mutation_types or normalized_entry_types is required for docket_stage_changes"
        )
    if len(requested_mutations) > MAX_MUTATION_TYPES:
        raise SchemaScopeError(
            f"mutation_types accepts at most {MAX_MUTATION_TYPES} exact variants"
        )
    if len(requested_entries) > MAX_NORMALIZED_ENTRY_TYPES:
        raise SchemaScopeError(
            "normalized_entry_types accepts at most "
            f"{MAX_NORMALIZED_ENTRY_TYPES} exact variants"
        )

    available_mutations = set(mutation_type_catalog(parameters))
    unknown_mutations = sorted(set(requested_mutations) - available_mutations)
    if unknown_mutations:
        raise SchemaScopeError(f"unknown mutation types: {', '.join(unknown_mutations)}")
    available_entries = set(normalized_entry_type_catalog(parameters))
    unknown_entries = sorted(set(requested_entries) - available_entries)
    if unknown_entries:
        raise SchemaScopeError(f"unknown normalized entry types: {', '.join(unknown_entries)}")

    scoped = copy.deepcopy(parameters)
    _strip_internal_properties(scoped)
    definitions = scoped["$defs"]
    patch_items = definitions["StagePatchInput"]["properties"]["operations"]["items"]
    patch_mapping = patch_items["discriminator"]["mapping"]
    selected_patch: dict[str, str] = {"draft_recompile": patch_mapping["draft_recompile"]}
    if requested_mutations:
        selected_patch["action_upsert"] = patch_mapping["action_upsert"]
        selected_patch["action_remove"] = patch_mapping["action_remove"]
        canonical_union = definitions["CanonicalChangeInput"]
        canonical_mapping = canonical_union["discriminator"]["mapping"]
        selected_canonical = {
            name: reference
            for name, reference in canonical_mapping.items()
            if name in requested_mutations
        }
        definitions["CanonicalChangeInput"] = _narrow_union(
            canonical_union, selected_canonical
        )
    if requested_entries:
        selected_patch["normalized_entry_upsert"] = patch_mapping[
            "normalized_entry_upsert"
        ]
        selected_patch["normalized_entry_remove"] = patch_mapping[
            "normalized_entry_remove"
        ]
        entry_schema = definitions["StageNormalizedEntryUpsert"]["properties"]["entry"]
        entry_mapping = entry_schema["discriminator"]["mapping"]
        selected_entries = {
            name: reference
            for name, reference in entry_mapping.items()
            if name in requested_entries
        }
        definitions["StageNormalizedEntryUpsert"]["properties"]["entry"] = _narrow_union(
            entry_schema, selected_entries
        )
    definitions["StagePatchInput"]["properties"]["operations"]["items"] = _narrow_union(
        patch_items, selected_patch
    )
    return _reference_closed(scoped)


def scoped_tool_description(
    tool_definition: dict[str, Any],
    mutation_types: Iterable[str] = (),
    *,
    normalized_entry_types: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a bounded, hash-bound description for one assembly tool scope."""
    function = tool_definition.get("function")
    if not isinstance(function, dict):
        raise SchemaScopeError("tool definition has no function object")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise SchemaScopeError("tool definition has no parameter schema")
    requested = tuple(dict.fromkeys(str(name).strip() for name in mutation_types if name))
    requested_entries = tuple(
        dict.fromkeys(str(name).strip() for name in normalized_entry_types if name)
    )
    tool_name = str(function.get("name") or "")
    if _is_commit_tool_name(tool_name):
        if requested or requested_entries:
            raise SchemaScopeError("commit accepts no mutation or entry schema scope; stage first")
        scoped = scoped_commit_schema(parameters)
    elif _is_clarification_tool_name(tool_name):
        scoped = scoped_clarification_schema(parameters, requested)
    elif _is_stage_tool_name(tool_name):
        scoped = scoped_stage_schema(
            parameters,
            mutation_types=requested,
            normalized_entry_types=requested_entries,
        )
    else:
        scoped = copy.deepcopy(parameters)
        _strip_internal_properties(scoped)
        scoped["additionalProperties"] = False
        scoped = _reference_closed(scoped)
    full_hash = hashlib.sha256(_canonical_json(parameters).encode()).hexdigest()
    scoped_hash = hashlib.sha256(_canonical_json(scoped).encode()).hexdigest()
    payload = {
        # Hermes describes deferred tools by their registry name, which is
        # namespaced for MCP tools. Preserve that exact callable name in the
        # response while keeping mutation semantics independent of the bridge.
        "name": tool_name,
        "description": function.get("description", ""),
        "schema_scope": {
            "mutation_types": list(requested),
            "normalized_entry_types": list(requested_entries),
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
        if not (name.startswith("docket_") or name.startswith("mcp__docket__docket_")):
            return original_dispatch(args, current_tool_defs=current_tool_defs)
        definition = _find_tool_definition(current_tool_defs, name)
        if definition is None:
            return original_dispatch(args, current_tool_defs=current_tool_defs)
        parameters = definition.get("function", {}).get("parameters", {})
        mutation_types = args.get(MUTATION_TYPES_ARGUMENT)
        requested_mutations = mutation_types if isinstance(mutation_types, list) else []
        entry_types = args.get(NORMALIZED_ENTRY_TYPES_ARGUMENT)
        requested_entries = entry_types if isinstance(entry_types, list) else []
        if "commit_mode" in args:
            return json.dumps({"error": "commit has no mode or payload; stage first, then commit"})
        if _is_clarification_tool_name(name) and not requested_mutations:
            return json.dumps({
                "error": "mutation_types is required to describe exact typed choices",
                "available_mutation_types": list(mutation_type_catalog(parameters)),
            })
        if _is_stage_tool_name(name) and not requested_mutations and not requested_entries:
            try:
                available_mutations = mutation_type_catalog(parameters)
                available_entries = normalized_entry_type_catalog(parameters)
            except SchemaScopeError as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)
            return json.dumps(
                {
                    "error": (
                        "mutation_types or normalized_entry_types is required when describing "
                        "docket_stage_changes; request only the forms needed by this patch"
                    ),
                    "available_mutation_types": list(available_mutations),
                    "available_normalized_entry_types": list(available_entries),
                },
                ensure_ascii=False,
            )
        try:
            payload = scoped_tool_description(
                definition,
                requested_mutations,
                normalized_entry_types=requested_entries,
            )
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
                    "For docket_stage_changes action patches or typed clarification choices, "
                    "the exact discriminated mutation_type values needed."
                ),
            }
            properties[NORMALIZED_ENTRY_TYPES_ARGUMENT] = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_NORMALIZED_ENTRY_TYPES,
                "description": (
                    "For docket_stage_changes, exact normalized entry forms needed by "
                    "this patch."
                ),
            }
            function["description"] = (
                str(function.get("description") or "")
                + " Scope Docket staging/clarification schemas to the required variants. "
                "Docket commit has no model arguments or mode."
            )
        return schemas

    tool_search.dispatch_tool_describe = dispatch_tool_describe
    tool_search.bridge_tool_schemas = bridge_tool_schemas
    tool_search._docket_scoped_describe_installed = True
    return True

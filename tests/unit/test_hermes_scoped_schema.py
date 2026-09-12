import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from docket.mcp.server import mcp

MODULE_PATH = Path("hermes/plugin/docket_discord/schema_disclosure.py")


def _module():
    spec = importlib.util.spec_from_file_location("scoped_schema_test_module", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _definition(
    tool_name: str, name: str | None = None,
) -> dict[str, object]:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools[tool_name]
    return {
        "type": "function",
        "function": {
            "name": name or tool_name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def _stage_definition(name: str = "docket_stage_changes") -> dict[str, object]:
    return _definition("docket_stage_changes", name)


def test_tracked_work_scope_is_exact_reference_closed_and_bounded() -> None:
    module = _module()
    definition = _definition("docket_request_clarification")
    full_schema = definition["function"]["parameters"]

    described = module.scoped_tool_description(
        definition,
        ["item_create", "task_create", "temporal_binding_create"],
    )

    scoped = described["parameters"]
    encoded = json.dumps(described, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(json.dumps(full_schema, separators=(",", ":")).encode()) > 100_000
    assert len(encoded) < 20_000
    assert described["schema_scope"]["complete_for_selected_mutations"] is True
    assert described["schema_scope"]["mutation_types"] == [
        "item_create",
        "task_create",
        "temporal_binding_create",
    ]
    content = scoped["$defs"]["OperatorChangeSetContent"]
    assert set(content["properties"]) == {
        "basis_refs",
        "expected_versions",
        "import_scope",
        "tracked_context_changes",
    }
    mapping = scoped["$defs"]["TrackedContextChangeInput"]["discriminator"]["mapping"]
    assert set(mapping) == {"item_create", "task_create", "temporal_binding_create"}
    assert "entry_coverage" in scoped["$defs"]["OperatorImportScope"]["properties"]
    assert "ImportEntryCoverage" in scoped["$defs"]
    assert "import_entry_id" in scoped["$defs"]["StatementInput"]["properties"]
    assert "EntityCreate" not in scoped["$defs"]
    assert "CanonicalEventCreate" not in scoped["$defs"]
    assert "assembly_operation_token" not in scoped["properties"]
    for definition_value in scoped["$defs"].values():
        for reference in module._definition_refs(definition_value):
            assert reference in scoped["$defs"]


def test_clarification_description_requires_an_explicit_known_scope() -> None:
    module = _module()
    definition = _definition("docket_request_clarification")
    with pytest.raises(module.SchemaScopeError, match="unknown mutation types"):
        module.scoped_tool_description(
            definition, ["made_up_create"]
        )


def test_commit_schema_has_no_model_arguments_and_hides_gateway_binding() -> None:
    module = _module()
    described = module.scoped_tool_description(
        _definition("docket_commit_changeset")
    )
    scoped = described["parameters"]
    assert len(json.dumps(described, separators=(",", ":")).encode()) < 3_000
    assert scoped["properties"] == {}
    assert not scoped.get("$defs")
    assert scoped["additionalProperties"] is False
    with pytest.raises(module.SchemaScopeError, match="commit accepts no mutation"):
        module.scoped_tool_description(_definition("docket_commit_changeset"), ["item_create"])


def test_normalized_entry_stage_schema_is_exact_and_bounded() -> None:
    module = _module()
    described = module.scoped_tool_description(
        _stage_definition(),
        normalized_entry_types=["scheduled_occurrence_entry"],
    )
    scoped = described["parameters"]
    assert len(json.dumps(described, separators=(",", ":")).encode()) < 16_000
    assert "assembly_argument_hash" not in scoped["properties"]
    mapping = scoped["$defs"]["StagePatchInput"]["properties"]["operations"][
        "items"
    ]["discriminator"]["mapping"]
    assert set(mapping) == {
        "normalized_entry_upsert", "normalized_entry_remove", "draft_recompile", "draft_adopt",
    }
    assert scoped["$defs"]["StageDraftAdopt"]["properties"].keys() == {"operation"}
    entry_mapping = scoped["$defs"]["StageNormalizedEntryUpsert"]["properties"][
        "entry"
    ]["discriminator"]["mapping"]
    assert set(entry_mapping) == {"scheduled_occurrence_entry"}
    assert "CanonicalChangeInput" not in scoped["$defs"]
    properties = scoped["$defs"]["ScheduledOccurrenceEntry"]["properties"]
    assert {"title", "timing", "lane_ref", "location", "evidence"} <= properties.keys()
    assert not {"item", "temporal", "calendar", "calendar_lane", "event_spec"} & properties.keys()


def test_pinned_hermes_bridge_requires_and_applies_mutation_scope(monkeypatch) -> None:
    module = _module()
    tool_search = ModuleType("tools.tool_search")

    def original_dispatch(args, *, current_tool_defs):
        del current_tool_defs
        return json.dumps({"original": args})

    def original_bridge_schemas(_deferred_count):
        return [
            {
                "type": "function",
                "function": {
                    "name": "tool_describe",
                    "description": "describe",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            }
        ]

    tool_search.dispatch_tool_describe = original_dispatch
    tool_search.bridge_tool_schemas = original_bridge_schemas
    tools_package = ModuleType("tools")
    tools_package.tool_search = tool_search
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.tool_search", tool_search)

    assert module.install_hermes_progressive_schema_patch() is True
    bridge = tool_search.bridge_tool_schemas(20)[0]["function"]
    assert "mutation_types" in bridge["parameters"]["properties"]
    assert "commit_mode" not in bridge["parameters"]["properties"]
    assert "normalized_entry_types" in bridge["parameters"]["properties"]

    missing_scope = json.loads(
        tool_search.dispatch_tool_describe(
            {"name": "docket_stage_changes"},
            current_tool_defs=[_stage_definition()],
        )
    )
    assert "mutation_types or normalized_entry_types is required" in missing_scope["error"]
    assert "item_create" in missing_scope["available_mutation_types"]

    described = json.loads(
        tool_search.dispatch_tool_describe(
            {
                "name": "docket_request_clarification",
                "mutation_types": [
                    "item_create",
                    "task_create",
                    "temporal_binding_create",
                ],
            },
            current_tool_defs=[_definition("docket_request_clarification")],
        )
    )
    assert described["schema_scope"]["complete_for_selected_mutations"] is True
    assert len(json.dumps(described, separators=(",", ":")).encode()) < 20_000


def test_pinned_hermes_bridge_scopes_namespaced_clarification_tool(monkeypatch) -> None:
    module = _module()
    tool_search = ModuleType("tools.tool_search")
    tool_search.dispatch_tool_describe = lambda args, *, current_tool_defs: json.dumps(
        {"original": args, "definitions": len(current_tool_defs)}
    )
    tool_search.bridge_tool_schemas = lambda _count: []
    tools_package = ModuleType("tools")
    tools_package.tool_search = tool_search
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.tool_search", tool_search)

    assert module.install_hermes_progressive_schema_patch() is True
    namespaced = "mcp__docket__docket_request_clarification"
    described = json.loads(
        tool_search.dispatch_tool_describe(
            {
                "name": namespaced,
                "mutation_types": [
                    "item_create",
                    "task_create",
                    "temporal_binding_create",
                ],
            },
            current_tool_defs=[_definition("docket_request_clarification", namespaced)],
        )
    )

    assert described["name"] == namespaced
    definitions = described["parameters"]["$defs"]
    assert set(definitions["TaskInput"]["properties"]) >= {
        "item_change_id",
        "title",
        "task_state",
    }
    assert set(definitions["TemporalBindingInput"]["properties"]) >= {
        "subject_change_id",
        "role",
        "temporal_value",
    }
    assert "status" not in definitions["TaskInput"]["properties"]
    assert "start_at" not in definitions["TemporalBindingInput"]["properties"]

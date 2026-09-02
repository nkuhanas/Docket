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


def _commit_definition() -> dict[str, object]:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools["docket_commit_changeset"]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def test_tracked_work_scope_is_exact_reference_closed_and_bounded() -> None:
    module = _module()
    definition = _commit_definition()
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
    assert "EntityCreate" not in scoped["$defs"]
    assert "CanonicalEventCreate" not in scoped["$defs"]
    for definition_value in scoped["$defs"].values():
        for reference in module._definition_refs(definition_value):
            assert reference in scoped["$defs"]


def test_commit_description_requires_an_explicit_known_scope() -> None:
    module = _module()
    definition = _commit_definition()
    with pytest.raises(module.SchemaScopeError, match="unknown mutation types"):
        module.scoped_tool_description(definition, ["made_up_create"])


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

    missing_scope = json.loads(
        tool_search.dispatch_tool_describe(
            {"name": "docket_commit_changeset"},
            current_tool_defs=[_commit_definition()],
        )
    )
    assert "mutation_types is required" in missing_scope["error"]
    assert "item_create" in missing_scope["available_mutation_types"]

    described = json.loads(
        tool_search.dispatch_tool_describe(
            {
                "name": "docket_commit_changeset",
                "mutation_types": [
                    "item_create",
                    "task_create",
                    "temporal_binding_create",
                ],
            },
            current_tool_defs=[_commit_definition()],
        )
    )
    assert described["schema_scope"]["complete_for_selected_mutations"] is True
    assert len(json.dumps(described, separators=(",", ":")).encode()) < 20_000

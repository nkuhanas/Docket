"""Measure proposed discovery consolidation; never execute a domain command.

This projects an inventory and routing map. It implements neither service
dispatch nor richer context, provider equivalence or compatibility handling.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]


def size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


async def probe():
    from mcp.types import ToolAnnotations
    from pydantic import Field, create_model

    from docket.config import Settings
    from docket.plugin.business import model_function
    from docket.plugin.server import PluginMCP
    from docket.schemas.business import BUSINESS_DEFINITIONS, CANONICAL_MUTATIONS
    from docket.schemas.native_commands import CommandBase

    def forbidden_callback(_arguments):
        raise RuntimeError("Design probe only: no command may execute.")

    def discovery(tools):
        return {
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools
            ]
        }

    server = PluginMCP(Settings(_env_file=None))
    baseline = deepcopy(await server.list_tools())
    by_name = {tool.name: tool for tool in baseline}
    retracts = {name: tool for name, tool in by_name.items() if name.startswith("docket_retract_")}
    reference_schema = None
    prefixes = []
    for tool in retracts.values():
        normalized = deepcopy(tool.input_schema)
        pattern = normalized["properties"]["target_ref"].pop("pattern")
        prefixes.append(pattern.removeprefix("^").split("_", 1)[0])
        if reference_schema is None:
            reference_schema = normalized
        assert normalized == reference_schema
    assert len(retracts) == 12 and len(set(prefixes)) == 12
    target_pattern = "^(?:" + "|".join(sorted(prefixes)) + ")_[0-9A-HJKMNP-TV-Z]{26}$"
    RetractRecord = create_model(
        "RetractRecord",
        __base__=CommandBase,
        target_ref=(Annotated[str, Field(pattern=target_pattern)], ...),
    )
    name = "docket_retract_record"
    server.authority_tools |= frozenset({name})
    server.add_tool(
        model_function(RetractRecord, name, forbidden_callback),
        name=name,
        description="Retract one supported canonical record with retained history. "
        "Docket derives its type/effect from target_ref. "
        "Calendar cancellation and identity unbinding remain separate.",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    replacement = next(tool for tool in await server.list_tools() if tool.name == name)
    normalized = deepcopy(replacement.input_schema)
    normalized["properties"]["target_ref"].pop("pattern")
    assert normalized == reference_schema
    assert size(replacement.input_schema) <= 1024
    assert '"oneOf"' not in json.dumps(replacement.input_schema)
    primary_removed = {*retracts, "docket_complete_task", "docket_get_conflict"}
    conditional_removed = {
        "docket_get_person_context",
        "docket_get_organization_or_institution_context",
        "docket_get_item_context",
        "docket_cancel_event",
    }
    scenarios = {}
    for scenario, removed in (
        ("primary", primary_removed),
        ("conditional", primary_removed | conditional_removed),
    ):
        assert removed <= set(by_name)
        projected = [tool for tool in baseline if tool.name not in removed] + [replacement]
        projected_by_name = {tool.name: tool for tool in projected}
        for tool in projected:
            if tool.name != name:
                assert tool.input_schema == by_name[tool.name].input_schema
        assert "docket_unbind_identity_binding" in projected_by_name
        scenarios[scenario] = {
            "tools": len(projected),
            "net_tools_removed": len(baseline) - len(projected),
            "input_bytes": sum(size(tool.input_schema) for tool in projected),
            "discovery_bytes": size(discovery(projected)),
            "unchanged_survivor_input_schemas": True,
            "removed_names": sorted(removed),
        }

    effects = {model.model_fields["mutation_type"].default for model in CANONICAL_MUTATIONS}
    effect_routes = {
        definition.mutation.model_fields["mutation_type"].default: (
            name if definition.tool_name in retracts else definition.tool_name
        )
        for definition in BUSINESS_DEFINITIONS
    }
    effect_routes.update(
        {
            "canonical_event_create": "docket_create_event",
            "canonical_event_modify": "docket_update_event",
            "canonical_event_cancel": "docket_cancel_event",
            "task_create": "docket_create_task",
            "task_modify": "docket_update_task",
        }
    )
    assert set(effect_routes) == effects
    primary_names = (set(by_name) - primary_removed) | {name}
    assert set(effect_routes.values()) <= primary_names

    work_metrics_path = ROOT / "evidence/work-attribution-contracts/metrics.json"
    work_metrics = json.loads(work_metrics_path.read_text())
    assert work_metrics["baseline"]["discovery_bytes"] == size(discovery(baseline))
    for values in scenarios.values():
        for label, phases in (("with_P0a", {"P0a"}), ("with_complete_P0", {"P0a", "P0b"})):
            additions = [row for row in work_metrics["candidates"] if row["phase"] in phases]
            values[label] = {
                "tools": values["tools"] + len(additions),
                "discovery_bytes": values["discovery_bytes"]
                + sum(row["discovery_bytes"] for row in additions)
                + len(additions),
            }

    print(
        json.dumps(
            {
                "kind": (
                    "synthetic inventory/routing projection; not implemented or runtime-verified"
                ),
                "source_commit": subprocess.check_output(
                    ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
                ).strip(),
                "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
                "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "work_measurement_sha256": hashlib.sha256(
                    work_metrics_path.read_bytes()
                ).hexdigest(),
                "measurement": (
                    "UTF-8 compact tools/list JSON; RPC envelope excluded; "
                    "surviving descriptions unchanged"
                ),
                "baseline": {
                    "tools": len(baseline),
                    "discovery_bytes": size(discovery(baseline)),
                    "input_bytes": sum(size(tool.input_schema) for tool in baseline),
                },
                "replacement": {
                    "tool": name,
                    "input_bytes": size(replacement.input_schema),
                    "definition_bytes": size(
                        replacement.model_dump(mode="json", by_alias=True, exclude_none=True)
                    ),
                    "fields": list(replacement.input_schema["properties"]),
                    "required": replacement.input_schema["required"],
                    "target_pattern": target_pattern,
                },
                "scenarios": scenarios,
                "primary_effect_routes": dict(sorted(effect_routes.items())),
                "baseline_inventory": [
                    {"tool": tool.name, "input_bytes": size(tool.input_schema)}
                    for tool in sorted(baseline, key=lambda tool: tool.name)
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    for name in tuple(os.environ):
        if name.startswith(("DOCKET_", "GOOGLE_", "HERMES_")):
            del os.environ[name]
    os.environ["DOCKET_ENVIRONMENT"] = "test"
    with TemporaryDirectory(prefix="docket-catalogue-design-") as working_directory:
        os.chdir(working_directory)
        asyncio.run(probe())

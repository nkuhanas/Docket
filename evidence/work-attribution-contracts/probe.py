"""Measure proposed work schemas through Docket's registration path; no commands run.

Design evidence only: these models have no services, validators or persistence and
are not imported by the application. Run with the repository's virtualenv Python.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

ROOT = Path(__file__).resolve().parents[2]


def size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


async def probe():
    from mcp.types import ToolAnnotations
    from pydantic import Field, create_model

    from docket.config import Settings
    from docket.plugin.business import model_function
    from docket.plugin.instructions import NATIVE_INSTRUCTIONS
    from docket.plugin.server import PluginMCP
    from docket.schemas.common import PublicRef, StrictModel
    from docket.schemas.native_commands import CommandBase, ContextId, EventRef, ItemRef, TaskRef
    from docket.schemas.registry import EntityRef

    WorkRef = Annotated[str, Field(pattern=r"^work_[0-9A-HJKMNP-TV-Z]{26}$")]
    EngagementRef = Annotated[str, Field(pattern=r"^eng_[0-9A-HJKMNP-TV-Z]{26}$")]
    AffiliationRef = Annotated[str, Field(pattern=r"^aff_[0-9A-HJKMNP-TV-Z]{26}$")]
    CompensationClass = Literal["billable", "paid_work", "unpaid", "unknown"]
    WorkMode = Literal["remote", "onsite", "travel"]

    ReportedBreak = create_model(
        "ReportedBreak",
        __base__=StrictModel,
        start=(str, Field(max_length=64)),
        end=(str, Field(max_length=64)),
    )
    details = {
        "description": (str, Field(min_length=1, max_length=4000)),
        "organization_ref": (EntityRef | None, None),
        "engagement_ref": (EngagementRef | None, None),
        "project_ref": (EntityRef | None, None),
        "item_ref": (ItemRef | None, None),
        "task_ref": (TaskRef | None, None),
        "event_ref": (EventRef | None, None),
        "place_ref": (EntityRef | None, None),
        "attribution_label": (str | None, Field(None, max_length=512)),
        "category": (str | None, Field(None, max_length=128)),
        "outcome": (str | None, Field(None, max_length=4000)),
        "reported_compensation_class": (CompensationClass | None, None),
        "work_mode": (WorkMode | None, None),
        "reported_location": (str | None, Field(None, max_length=1000)),
        "source_refs": (list[PublicRef], Field(default_factory=list, max_length=25)),
        "source_method": (Literal["report", "import", "inference"], "report"),
        "uncertainty": (str | None, Field(None, max_length=1000)),
    }
    LogWork = create_model(
        "LogWork",
        __base__=CommandBase,
        **details,
        start=(str | None, Field(None, max_length=64, description="ISO timestamp; pair with end.")),
        end=(str | None, Field(None, max_length=64, description="ISO timestamp after start.")),
        work_date=(date | None, None),
        period_start=(date | None, None),
        period_end=(date | None, None),
        duration_minutes=(
            Decimal | None,
            Field(
                None,
                gt=0,
                description="Reported quantity; no invented clock bounds.",
            ),
        ),
        timezone=(str | None, Field(None, max_length=128)),
        breaks=(list[ReportedBreak], Field(default_factory=list, max_length=25)),
    )

    def sparse(model, name, ref_name, ref_type):
        fields = {}
        for key, field in model.model_fields.items():
            if key in CommandBase.model_fields:
                continue
            optional = deepcopy(field)
            optional.default, optional.default_factory = None, None
            fields[key] = (field.annotation | None, optional)
        fields[ref_name] = (ref_type, ...)
        return create_model(name, __base__=CommandBase, **fields)

    UpdateWork = sparse(LogWork, "UpdateWork", "work_ref", WorkRef)
    # Retraction is a named lifecycle change on the existing update, not another tool.
    UpdateWork = create_model(
        "UpdateWork",
        __base__=UpdateWork,
        lifecycle=(Literal["recorded", "retracted"] | None, None),
    )
    CreateEngagement = create_model(
        "CreateEngagement",
        __base__=CommandBase,
        label=(str, Field(min_length=1, max_length=512)),
        kind=(Literal["employment", "contract", "research", "volunteer", "personal"], ...),
        organization_ref=(EntityRef | None, None),
        payer_ref=(EntityRef | None, None),
        affiliation_ref=(AffiliationRef | None, None),
        effective_start=(date | None, None),
        effective_end=(date | None, None),
        role=(str | None, Field(None, max_length=512)),
        reporting_notes=(str | None, Field(None, max_length=4000)),
    )
    UpdateEngagement = sparse(
        CreateEngagement,
        "UpdateEngagement",
        "engagement_ref",
        EngagementRef,
    )
    GetWorkContext = create_model(
        "GetWorkContext",
        __base__=StrictModel,
        engagement_ref=(EngagementRef | None, None),
        work_refs=(list[WorkRef], Field(default_factory=list, max_length=25)),
        search=(str | None, Field(None, max_length=200)),
    )
    QueryWork = create_model(
        "QueryWork",
        __base__=StrictModel,
        context_id=(ContextId | None, None),
        period_start=(date | None, None),
        period_end=(date | None, None),
        timezone=(str | None, Field(None, max_length=128)),
        engagement_ref=(EngagementRef | None, None),
        organization_ref=(EntityRef | None, None),
        project_ref=(EntityRef | None, None),
        work_ref=(WorkRef | None, None),
        lifecycle=(Literal["running", "paused", "recorded", "retracted"] | None, None),
        reported_compensation_class=(CompensationClass | None, None),
        work_mode=(WorkMode | None, None),
        group_by=(Literal["engagement", "project", "day", "week"], "engagement"),
        output_format=(Literal["json", "csv"], "json"),
        include_unplaced=(bool, True),
        limit=(int, Field(25, ge=1, le=100)),
        cursor=(str | None, Field(None, max_length=256)),
    )
    StartWork = create_model("StartWork", __base__=CommandBase, **details)
    TimerTarget = create_model("TimerTarget", __base__=CommandBase, work_ref=(WorkRef, ...))
    StopWork = create_model(
        "StopWork",
        __base__=TimerTarget,
        outcome=(str | None, Field(None, max_length=4000)),
    )
    SwitchWork = create_model(
        "SwitchWork",
        __base__=StartWork,
        from_work_ref=(WorkRef, ...),
        previous_outcome=(str | None, Field(None, max_length=4000)),
    )
    WorkPart = create_model(
        "WorkPart",
        __base__=StrictModel,
        start=(str | None, Field(None, max_length=64)),
        end=(str | None, Field(None, max_length=64)),
        duration_minutes=(Decimal | None, Field(None, gt=0)),
        engagement_ref=(EngagementRef | None, None),
        project_ref=(EntityRef | None, None),
    )
    SplitWork = create_model(
        "SplitWork",
        __base__=TimerTarget,
        parts=(list[WorkPart], Field(min_length=2, max_length=25)),
    )
    MergeWork = create_model(
        "MergeWork",
        __base__=CommandBase,
        work_refs=(list[WorkRef], Field(min_length=2, max_length=25)),
        engagement_ref=(EngagementRef | None, None),
        project_ref=(EntityRef | None, None),
        description=(str | None, Field(None, max_length=4000)),
    )
    candidates = [
        (
            "P0a",
            "docket_get_work_context",
            GetWorkContext,
            True,
            "Read bounded work choices, selected sessions, versions and active timer.",
        ),
        (
            "P0a",
            "docket_query_work_sessions",
            QueryWork,
            True,
            "Query actual-work totals/details; output_format selects informal JSON or CSV.",
        ),
        (
            "P0a",
            "docket_log_work_session",
            LogWork,
            False,
            "Record actual work with intervals, a reported duration or incomplete time. "
            "Reported billability is not verified eligibility.",
        ),
        (
            "P0a",
            "docket_update_work_session",
            UpdateWork,
            False,
            "Correct supplied fields on selected work; null clears optional values. "
            "lifecycle=retracted preserves history.",
        ),
        (
            "P0a",
            "docket_create_engagement",
            CreateEngagement,
            False,
            "Record an employment, contract or unpaid arrangement; "
            "rates are optional later policies.",
        ),
        (
            "P0a",
            "docket_update_engagement",
            UpdateEngagement,
            False,
            "Correct supplied engagement fields using context-held versions; "
            "null clears optional values.",
        ),
        (
            "P0b",
            "docket_start_work_session",
            StartWork,
            False,
            "Start one primary timer at server now, captured once per operation_id.",
        ),
        (
            "P0b",
            "docket_pause_work_session",
            TimerTarget,
            False,
            "Pause the selected active timer at a retry-stable server instant.",
        ),
        (
            "P0b",
            "docket_resume_work_session",
            TimerTarget,
            False,
            "Resume the selected paused timer without inventing work during its break.",
        ),
        (
            "P0b",
            "docket_stop_work_session",
            StopWork,
            False,
            "Stop the selected timer and optionally record an outcome; retain its segments.",
        ),
        (
            "P0b",
            "docket_switch_work_session",
            SwitchWork,
            False,
            "Atomically stop from_work_ref and start the described work at one captured instant.",
        ),
        (
            "P0b",
            "docket_split_work_session",
            SplitWork,
            False,
            "Split selected work into bounded parts; "
            "Docket preserves quantities, breaks and claim lineage.",
        ),
        (
            "P0b",
            "docket_merge_work_sessions",
            MergeWork,
            False,
            "Merge selected work while preserving segments and gaps; "
            "resolve incompatible attribution explicitly.",
        ),
    ]

    def forbidden_callback(_arguments):
        raise RuntimeError("Design probe only: no work commands may execute.")

    def discovery(tools):
        return {
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools
            ]
        }

    server = PluginMCP(Settings(_env_file=None))
    baseline = deepcopy(await server.list_tools())
    baseline_by_name = {tool.name: tool for tool in baseline}
    server.authority_tools |= frozenset(name for _, name, _, read, _ in candidates if not read)
    for _, name, model, read, description in candidates:
        server.add_tool(
            model_function(model, name, forbidden_callback),
            name=name,
            description=description,
            annotations=ToolAnnotations(
                read_only_hint=read,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=not read,
            ),
        )
    proposed = {
        tool.name: tool for tool in await server.list_tools() if tool.name not in baseline_by_name
    }
    rows = []
    for phase, name, _, read, _ in candidates:
        tool = proposed[name]
        schema = tool.input_schema
        assert size(schema) <= 4096, (name, size(schema))
        assert size(tool.model_dump(mode="json", by_alias=True, exclude_none=True)) <= 6144
        assert len(schema.get("$defs", {})) <= 1
        assert "mutation_type" not in json.dumps(schema)
        assert '"oneOf"' not in json.dumps(schema)
        assert '"discriminator"' not in json.dumps(schema)
        assert "work" not in schema["properties"]
        assert schema["additionalProperties"] is False
        rows.append(
            {
                "phase": phase,
                "tool": name,
                "read_only": read,
                "input_bytes": size(schema),
                "discovery_bytes": size(
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                ),
                "fields": len(schema["properties"]),
                "required": schema.get("required", []),
                "definitions": len(schema.get("$defs", {})),
            }
        )
    extension = {
        phase: {
            "tools": sum(row["phase"] == phase for row in rows),
            "input_bytes": sum(row["input_bytes"] for row in rows if row["phase"] == phase),
            "discovery_bytes": sum(row["discovery_bytes"] for row in rows if row["phase"] == phase),
        }
        for phase in ("P0a", "P0b")
    }
    assert extension["P0a"]["discovery_bytes"] <= 20 * 1024
    assert sum(row["discovery_bytes"] for row in rows) <= 32 * 1024
    result = {
        "kind": "schema feasibility only; no runtime work/finance implementation",
        "source_commit": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "measurement": (
            "UTF-8 compact JSON; ensure_ascii=False; tools/list result without RPC envelope"
        ),
        "baseline": {
            "tools": len(baseline),
            "input_bytes": sum(size(t.input_schema) for t in baseline),
            "discovery_bytes": size(discovery(baseline)),
            "largest_input_bytes": max(size(t.input_schema) for t in baseline),
            "global_instruction_bytes": len(NATIVE_INSTRUCTIONS.encode("utf-8")),
        },
        "extension": extension,
        "candidates": rows,
        "combined_discovery_bytes": size(discovery([*baseline, *proposed.values()])),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    for name in tuple(os.environ):
        if name.startswith(("DOCKET_", "GOOGLE_", "HERMES_")):
            del os.environ[name]
    os.environ["DOCKET_ENVIRONMENT"] = "test"
    # Relative .env/credential paths cannot resolve to workspace production data.
    with TemporaryDirectory(prefix="docket-contract-design-") as working_directory:
        os.chdir(working_directory)
        asyncio.run(probe())

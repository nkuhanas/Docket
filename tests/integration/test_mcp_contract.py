import hashlib
from pathlib import Path

import pytest
import yaml

from docket.mcp.server import mcp
from docket.mcp.triage_server import triage_mcp
from docket.tool_contracts import (
    CONTRACT_ENTRIES,
    contract_hash,
    contract_tool_names,
    render_contract,
)

INTERACTIVE_TOOLS = {
    "docket_commit_changeset",
    "docket_get_calendar_profile",
    "docket_get_calendar_sync_status",
    "docket_get_conflict",
    "docket_get_history_entry",
    "docket_get_intent_session",
    "docket_get_network_neighborhood",
    "docket_get_organization_context",
    "docket_get_person_context",
    "docket_get_queue_item",
    "docket_get_record",
    "docket_get_triage_case",
    "docket_list_accounts",
    "docket_list_calendar_events",
    "docket_list_calendar_lanes",
    "docket_list_queue_items",
    "docket_list_reminder_rules",
    "docket_network_search",
    "docket_query_people",
    "docket_resolve_conflict",
    "docket_search_history",
    "docket_search_records",
}

TRIAGE_TOOLS = {
    "docket_get_triage_context",
    "docket_submit_triage_analysis",
    "docket_get_triage_case",
    "docket_apply_existing_suppression",
}

REMOVED_LEGACY_MUTATIONS = {
    "docket_add_entity_alias",
    "docket_apply_calendar_intent",
    "docket_apply_course_intent",
    "docket_archive_record",
    "docket_configure_calendar_lane",
    "docket_create_entity",
    "docket_delete_calendar_lane",
    "docket_ignore_queue_item",
    "docket_merge_entities",
    "docket_migrate_calendar_events",
    "docket_rebind_entity_resolution",
    "docket_relate_entities",
    "docket_restore_record",
    "docket_retract_entity_relation",
    "docket_set_calendar_profile",
    "docket_snooze_queue_item",
    "docket_store_record",
    "docket_update_entity",
    "docket_update_entity_relation",
    "docket_update_record",
}

REPLACED_LEGACY_READS = {
    "docket_get_entity",
    "docket_resolve_entity",
    "docket_search_entities",
}


@pytest.mark.asyncio
async def test_interactive_profile_exposes_only_reads_and_changeset_authority() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools) == INTERACTIVE_TOOLS
    assert REMOVED_LEGACY_MUTATIONS.isdisjoint(tools)
    assert REPLACED_LEGACY_READS.isdisjoint(tools)
    assert "atomically" in (tools["docket_commit_changeset"].description or "")
    assert "Conflict" in (tools["docket_resolve_conflict"].description or "")
    commit_schema = tools["docket_commit_changeset"].inputSchema
    assert {
        "utterance_ref",
        "statements",
        "relations",
        "resolved_intent",
        "blocking_clarifications",
        "content",
        "request_key",
        "source",
        "actor_id",
    }.issubset(commit_schema["properties"])
    content = commit_schema["$defs"]["ChangeSetContent"]
    assert {
        "registry_changes",
        "preference_changes",
        "lane_changes",
        "event_changes",
        "resolution_changes",
        "provider_intents",
    }.issubset(content["properties"])
    for name in {
        "docket_list_calendar_lanes",
        "docket_list_calendar_events",
        "docket_get_calendar_sync_status",
        "docket_list_reminder_rules",
    }:
        schema = tools[name].inputSchema
        assert "account_ref" in schema["properties"]
        assert "account_id" not in schema["properties"]
    assert "record_key" in tools["docket_get_record"].inputSchema["properties"]
    assert "item_ref" in tools["docket_get_queue_item"].inputSchema["properties"]


@pytest.mark.asyncio
async def test_triage_profile_is_exact_and_non_authoritative() -> None:
    tools = {tool.name: tool for tool in await triage_mcp.list_tools()}
    assert set(tools) == TRIAGE_TOOLS
    submit = tools["docket_submit_triage_analysis"].inputSchema
    serialized = repr(submit)
    assert "registry_changes" not in serialized
    assert "provider_intents" not in serialized
    assert "semantic_classes" in submit["properties"]


def test_generated_tool_contracts_have_exact_profile_parity_and_hashes() -> None:
    expected_fields = {
        "tool_ref",
        "tool_name",
        "purpose",
        "use_when",
        "do_not_use_when",
        "authority",
        "preconditions",
        "side_effects",
        "success_dispositions",
        "output_interpretation",
        "required_next_action",
        "important_errors",
    }
    interactive_path = Path("hermes/plugin/docket_discord/contracts/interactive.md")
    triage_path = Path("hermes/plugin/docket_discord/contracts/triage.md")
    interactive = interactive_path.read_text(encoding="utf-8")
    triage = triage_path.read_text(encoding="utf-8")
    assert interactive == render_contract("interactive")
    assert triage == render_contract("triage")
    assert len(interactive.encode("utf-8")) <= 24 * 1024
    assert len(triage.encode("utf-8")) <= 12 * 1024
    assert f"contract_hash: {contract_hash('interactive')}" in interactive
    assert f"contract_hash: {contract_hash('triage')}" in triage
    assert contract_tool_names("interactive") == INTERACTIVE_TOOLS
    assert contract_tool_names("triage") == TRIAGE_TOOLS
    assert contract_tool_names("interactive") & contract_tool_names("triage") == {
        "docket_get_triage_case"
    }
    for entries in CONTRACT_ENTRIES.values():
        assert len(entries) == len({entry["tool_name"] for entry in entries})
        for entry in entries:
            assert set(entry) == expected_fields
            assert all(str(value).strip() for value in entry.values())


def test_frozen_34_plus_4_migration_matrix_is_complete_and_targets_current_contracts() -> None:
    matrix_path = Path("deltas/docket-tool-migration-matrix-08-27-2026.yaml")
    readiness = yaml.safe_load(
        Path("deltas/docket-ontology-readiness-status-08-27-2026.yaml").read_bytes()
    )
    blocker = next(
        item
        for item in readiness["implementation_start_blockers"]
        if item["blocker_ref"] == "ONT-OPEN-0002"
    )
    assert blocker["status"] == "resolved"
    assert blocker["evidence"]["path"] == str(matrix_path)
    assert blocker["evidence"]["sha256"] == (
        "3933b8a0ed305e348224072b48219a2897389a3dc1d2652fbdfbc5bd48c7f42f"
    )
    assert blocker["summary"] == {
        "interactive_tools": 34,
        "triage_tools": 4,
        "replace": 28,
        "modify": 10,
        "retain": 0,
        "remove": 0,
    }

    # The source migration handoff is private provenance and is intentionally
    # absent from a clean GitHub checkout. Validate it byte-for-byte when the
    # operator's checkout has it, while keeping CI anchored to the checked-in
    # readiness record and current generated contracts.
    if not matrix_path.is_file():
        return

    raw = matrix_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "3933b8a0ed305e348224072b48219a2897389a3dc1d2652fbdfbc5bd48c7f42f"
    )
    matrix = yaml.safe_load(raw)
    rows = matrix["tools"]
    assert len(rows) == 38
    assert len({row["current_tool_name"] for row in rows}) == 38
    assert sum(row["current_profile"] == "interactive" for row in rows) == 34
    assert sum(row["current_profile"] == "triage" for row in rows) == 4
    assert sum(row["disposition"] == "replace" for row in rows) == 28
    assert sum(row["disposition"] == "modify" for row in rows) == 10

    current_targets = INTERACTIVE_TOOLS | TRIAGE_TOOLS
    for row in rows:
        assert row["disposition"] in {"retain", "modify", "replace", "remove"}
        assert row["target_contract"]
        if row["disposition"] in {"modify", "replace"}:
            assert current_targets.intersection(row["target_tool_names"]), row

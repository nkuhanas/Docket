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
    "docket_get_attention_case",
    "docket_get_calendar_sync_status",
    "docket_get_conflict",
    "docket_get_context_neighborhood",
    "docket_get_history_entry",
    "docket_get_intent_session",
    "docket_get_item_context",
    "docket_get_organization_or_institution_context",
    "docket_get_person_context",
    "docket_list_calendar_lanes",
    "docket_list_provider_accounts",
    "docket_list_provider_calendar_events",
    "docket_list_reminder_plans",
    "docket_query_items",
    "docket_query_people",
    "docket_resolve_conflict",
    "docket_search_entities",
    "docket_search_history",
}

TRIAGE_TOOLS = {
    "docket_get_triage_context",
    "docket_submit_triage_analysis",
    "docket_get_attention_case",
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
        }.issubset(commit_schema["properties"])
    content = commit_schema["$defs"]["OperatorChangeSetContent"]
    assert {
        "registry_changes",
        "preference_changes",
        "lane_changes",
        "event_changes",
        "resolution_changes",
    }.issubset(content["properties"])
    assert "provider_intents" not in content["properties"]
    assert "ProviderIntentInput" not in commit_schema["$defs"]
    registry_union = commit_schema["$defs"]["RegistryChangeInput"]
    assert registry_union["discriminator"]["propertyName"] == "mutation_type"
    assert registry_union["discriminator"]["mapping"]["entity_create"] == (
        "#/$defs/EntityCreate"
    )
    assert registry_union["discriminator"]["mapping"]["identity_binding_bind"] == (
        "#/$defs/IdentityBindingBind"
    )
    identity_bind = commit_schema["$defs"]["IdentityBindingBind"]
    assert identity_bind["additionalProperties"] is False
    assert "object_change_id" in identity_bind["properties"]
    assert identity_bind["properties"]["payload"] == {
        "$ref": "#/$defs/IdentityBindingBindSpec"
    }
    identity_bind_spec = commit_schema["$defs"]["IdentityBindingBindSpec"]
    assert "entity_change_id" in identity_bind_spec["properties"]
    assert identity_bind_spec["properties"]["resolution_basis"] == {
        "$ref": "#/$defs/IdentityResolutionBasis"
    }
    assert "*_change_id" in (tools["docket_commit_changeset"].description or "")
    case_resolution = commit_schema["$defs"]["ResolutionChangeInput"]
    assert case_resolution["additionalProperties"] is False
    assert case_resolution["properties"]["object_ref"]["pattern"].startswith("^case_")
    assert case_resolution["properties"]["case_revision_ref"]["pattern"].startswith(
        "^caserev_"
    )
    assert "payload" not in case_resolution["properties"]
    assert "affected_fields" not in case_resolution["properties"]
    assert "ConflictResolution" not in repr(commit_schema)

    lane_create = commit_schema["$defs"]["CalendarLaneCreateSpec"]
    assert "account_ref" in lane_create["properties"]
    assert "account_id" not in lane_create["properties"]
    assert "provider_calendar_binding" not in lane_create.get("required", [])

    conflict_schema = tools["docket_resolve_conflict"].inputSchema
    canonical_effects = conflict_schema["properties"]["canonical_effects"]
    assert canonical_effects["items"] == {"$ref": "#/$defs/CanonicalChangeInput"}
    canonical_union = conflict_schema["$defs"]["CanonicalChangeInput"]
    assert canonical_union["discriminator"]["propertyName"] == "mutation_type"
    assert "conflict_resolution" not in canonical_union["discriminator"]["mapping"]
    for name in {
        "docket_list_calendar_lanes",
        "docket_list_provider_calendar_events",
        "docket_get_calendar_sync_status",
    }:
        schema = tools[name].inputSchema
        assert "account_ref" in schema["properties"]
        assert "account_id" not in schema["properties"]
    assert "subject_ref" in tools["docket_list_reminder_plans"].inputSchema["properties"]
    calendar_events = tools["docket_list_provider_calendar_events"]
    assert "calendar_id" not in calendar_events.inputSchema.get("required", [])
    assert "globally ordered" in (calendar_events.description or "")
    history_type = tools["docket_search_history"].inputSchema["properties"][
        "object_type"
    ]
    history_type_schema = next(
        branch for branch in history_type["anyOf"] if "enum" in branch
    )
    assert set(history_type_schema["enum"]) >= {
        "operator_utterance",
        "attention_case",
        "tool_invocation",
        "runtime_log_entry",
    }
    assert "item_ref" in tools["docket_get_item_context"].inputSchema["properties"]
    assert "context_entity_ref" in tools["docket_query_items"].inputSchema["properties"]


@pytest.mark.asyncio
async def test_triage_profile_is_exact_and_non_authoritative() -> None:
    tools = {tool.name: tool for tool in await triage_mcp.list_tools()}
    assert set(tools) == TRIAGE_TOOLS
    submit = tools["docket_submit_triage_analysis"].inputSchema
    serialized = repr(submit)
    assert "registry_changes" not in serialized
    assert "provider_intents" not in serialized
    assert "semantic_classes" in submit["properties"]
    case_item = submit["$defs"]["CaseItemInput"]
    assert "resolution_role" in case_item["required"]
    assert case_item["properties"]["resolution_role"]["enum"] == [
        "required",
        "supporting",
    ]


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
        "docket_get_attention_case"
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

    for row in rows:
        assert row["disposition"] in {"retain", "modify", "replace", "remove"}
        assert row["target_contract"]

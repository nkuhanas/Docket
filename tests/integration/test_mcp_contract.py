import ast
import runpy
from pathlib import Path

import pytest

from docket.mcp import mcp, triage_mcp
from docket.services.mcp_traces import DOCKET_MCP_TOOL_NAMES


def _plugin_trace_tools() -> set[str]:
    tree = ast.parse(
        Path("hermes/plugin/docket_discord/__init__.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_DOCKET_MCP_TOOL_NAMES"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and node.value.args
        ):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError("Discord plugin MCP trace allowlist was not found")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_tools_and_active_template_allowlist_move_together() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert names == {
        "docket_add_entity_alias",
        "docket_create_entity",
        "docket_store_record",
        "docket_get_record",
        "docket_search_records",
        "docket_update_record",
        "docket_archive_record",
        "docket_restore_record",
        "docket_list_accounts",
        "docket_list_calendar_lanes",
        "docket_configure_calendar_lane",
        "docket_delete_calendar_lane",
        "docket_list_calendar_events",
        "docket_get_calendar_sync_status",
        "docket_get_calendar_profile",
        "docket_get_entity",
        "docket_set_calendar_profile",
        "docket_list_reminder_rules",
        "docket_merge_entities",
        "docket_migrate_calendar_events",
        "docket_apply_calendar_intent",
        "docket_apply_course_intent",
        "docket_rebind_entity_resolution",
        "docket_relate_entities",
        "docket_retract_entity_relation",
        "docket_resolve_entity",
        "docket_search_entities",
        "docket_list_queue_items",
        "docket_get_queue_item",
        "docket_snooze_queue_item",
        "docket_ignore_queue_item",
        "docket_get_action",
        "docket_update_entity",
        "docket_update_entity_relation",
    }
    assert not names.intersection(
        {"record_approval", "consume_approval", "execute_action", "raw_gmail_modify"}
    )
    template = Path("hermes/config.example.yaml").read_text(encoding="utf-8")
    include_block = template.split("    tools:\n      include:\n", 1)[1].split("      prompts:", 1)[
        0
    ]
    configured_names = {
        line.removeprefix("        - ").strip()
        for line in include_block.splitlines()
        if line.strip()
    }
    assert configured_names == names
    assert names == DOCKET_MCP_TOOL_NAMES
    assert _plugin_trace_tools() == names
    smoke_contract = runpy.run_path("scripts/compose-mcp-smoke.py")
    assert smoke_contract["EXPECTED_TOOLS"] == names
    store_description = " ".join((tools["docket_store_record"].description or "").split())
    assert "not Hermes memory" in store_description
    assert "even when search found" in store_description
    assert "attaching the current source provenance" in store_description
    assert "record_conflict" in store_description
    assert "Never copy the existing record" in store_description
    assert "docket_update_record" in store_description

    entity_search = tools["docket_search_entities"]
    entity_search_description = " ".join((entity_search.description or "").split())
    assert "before asking the operator for known facts" in entity_search_description
    assert "subject predicate object" in entity_search_description
    entity_search_properties = entity_search.inputSchema["properties"]
    assert entity_search_properties["limit"]["maximum"] == 50
    assert entity_search_properties["predicate"]["anyOf"][0]["enum"] == [
        "works_for",
        "member_of",
        "affiliated_with",
        "advises",
        "instructs",
        "reports_to",
        "collaborates_with",
        "knows",
        "friend_of",
        "classmate_of",
        "leads",
        "participates_in",
        "located_at",
        "uses",
        "supports",
    ]
    assert entity_search_properties["direction"]["enum"] == ["any", "subject", "object"]

    create_entity = tools["docket_create_entity"]
    entity_attributes = create_entity.inputSchema["$defs"]["EntityAttributes"]
    assert entity_attributes["additionalProperties"] is False
    assert "is_operator" in entity_attributes["properties"]
    assert "email_addresses" in entity_attributes["properties"]
    lane_default = entity_attributes["properties"]["calendar_lane_default"]["anyOf"][0]
    assert lane_default["pattern"] == "^[a-z0-9][a-z0-9_-]*$"

    configure_lane = tools["docket_configure_calendar_lane"]
    assert configure_lane.inputSchema["properties"]["lane"]["pattern"] == (
        "^[a-z0-9][a-z0-9_-]*$"
    )
    configure_description = " ".join((configure_lane.description or "").split())
    assert "explicitly asks" in configure_description
    assert "never deletes" in configure_description

    calendar_intent = tools["docket_apply_calendar_intent"]
    standalone_event = calendar_intent.inputSchema["$defs"]["StandaloneCalendarEventInput"]
    assert standalone_event["properties"]["calendar_lane"]["pattern"] == (
        "^[a-z0-9][a-z0-9_-]*$"
    )
    update_entity = tools["docket_update_entity"]
    update_entity_description = " ".join((update_entity.description or "").split())
    assert "preserves all other metadata" in update_entity_description
    assert "attributes" not in update_entity.inputSchema["properties"]
    assert "attribute_updates" in update_entity.inputSchema["properties"]
    relation_schema = tools["docket_relate_entities"].inputSchema
    assert relation_schema["properties"]["predicate"]["enum"] == (
        entity_search_properties["predicate"]["anyOf"][0]["enum"]
    )
    search_description = " ".join((tools["docket_search_records"].description or "").split())
    assert "before answering operational facts" in search_description
    assert "Never claim a store/save/remember request succeeded" in search_description

    store_schema = tools["docket_store_record"].inputSchema
    properties = store_schema["properties"]
    definitions = store_schema["$defs"]
    assert properties["record_type"]["enum"] == ["term", "course", "generic"]
    assert properties["request_key"]["pattern"].startswith("^discord:")
    assert properties["actor_id"]["pattern"] == "^[0-9]{17,20}$"
    assert definitions["TermData"]["additionalProperties"] is False
    assert definitions["TermData"]["required"] == ["institution", "term_name"]
    assert definitions["CourseData"]["additionalProperties"] is False
    assert definitions["CourseData"]["required"] == ["term_record_id", "course_code"]
    meetings_schema = definitions["CourseData"]["properties"]["meetings"]
    assert "stable descriptive meeting ID" in meetings_schema["description"]
    assert meetings_schema["examples"][0]["lecture-fr-1"]["days"] == ["FR"]
    assert meetings_schema["patternProperties"]
    assert definitions["CourseMeeting"]["additionalProperties"] is False
    assert definitions["CourseMeeting"]["properties"]["days"]["items"]["enum"] == [
        "MO",
        "TU",
        "WE",
        "TH",
        "FR",
        "SA",
        "SU",
    ]
    assert definitions["RecordSourceInput"]["properties"]["source_type"]["const"] == (
        "discord_message"
    )
    source_metadata = definitions["DiscordSourceMetadata"]["properties"]
    assert "Docket-owned daily thread" in source_metadata["parent_channel_id"]["description"]
    snooze = tools["docket_snooze_queue_item"]
    snooze_description = " ".join((snooze.description or "").split())
    assert "configured local daily rollover hour" in snooze_description
    assert "never mutates Gmail or Calendar" in snooze_description
    snooze_properties = snooze.inputSchema["properties"]
    assert snooze_properties["request_key"]["pattern"].startswith("^discord:")
    assert "snoozed_until" in snooze_properties
    assert "snooze_local_date" in snooze_properties

    list_queue_properties = tools["docket_list_queue_items"].inputSchema["properties"]
    assert list_queue_properties["source_item_id"]["anyOf"][0]["format"] == "uuid"

    ignore_description = " ".join((tools["docket_ignore_queue_item"].description or "").split())
    assert "without mutating its source" in ignore_description

    calendar_lookup = tools["docket_list_calendar_events"]
    lookup_description = " ".join((calendar_lookup.description or "").split())
    assert "maximum is 31 days" in lookup_description
    assert "do not use a terminal or another clock" in lookup_description
    assert "never call a terminal to convert event times" in lookup_description
    assert "Use ``require_fresh`` for direct current" in lookup_description
    assert "newly added provider event" in lookup_description
    assert "never expose descriptions, attendees, conference data" in lookup_description
    lookup_properties = calendar_lookup.inputSchema["properties"]
    assert lookup_properties["freshness"]["enum"] == ["prefer_cache", "require_fresh"]
    assert lookup_properties["relative_day"]["anyOf"][0]["enum"] == ["today", "tomorrow"]
    assert lookup_properties["limit"]["maximum"] == 100
    assert lookup_properties["result_view"]["enum"] == ["occurrences", "series"]
    assert "one compact, directly reusable provider identity" in lookup_description

    get_action = tools["docket_get_action"]
    get_action_description = " ".join((get_action.description or "").split())
    get_action_properties = get_action.inputSchema["properties"]
    assert get_action_properties["detail"]["enum"] == ["status", "full"]
    assert get_action_properties["wait_seconds"]["maximum"] == 30
    assert "replace repeated model-mediated polling" in get_action_description

    lane_move_description = " ".join(
        (tools["docket_migrate_calendar_events"].description or "").split()
    )
    assert "queues the immutable items directly" in lane_move_description
    assert "no approval proposal is created" in lane_move_description
    lane_delete_description = " ".join(
        (tools["docket_delete_calendar_lane"].description or "").split()
    )
    assert "does not create an approval proposal" in lane_delete_description

    calendar_proposal_definitions = tools["docket_apply_calendar_intent"].inputSchema["$defs"]
    calendar_proposal_description = " ".join(
        (tools["docket_apply_calendar_intent"].description or "").split()
    )
    assert "inherits Docket's configured ``DOCKET_TIMEZONE``" in (calendar_proposal_description)
    timed_timing = calendar_proposal_definitions["TimedEventTiming"]
    assert "timezone" not in timed_timing["required"]
    assert "DOCKET_TIMEZONE" in timed_timing["properties"]["timezone"]["description"]
    all_day_timing = calendar_proposal_definitions["AllDayEventTiming"]
    assert "timezone" not in all_day_timing["required"]

    list_rules = tools["docket_list_reminder_rules"]
    list_rules_description = " ".join((list_rules.description or "").split())
    assert "rather than conversational memory or a past-session search" in (list_rules_description)
    list_rules_properties = list_rules.inputSchema["properties"]
    assert list_rules_properties["limit"]["maximum"] == 100

    calendar_proposal = tools["docket_apply_calendar_intent"]
    calendar_proposal_description = " ".join((calendar_proposal.description or "").split())
    assert "create, update, reminder change, or cancellation" in (calendar_proposal_description)
    assert "only when ``docket_queue`` is enabled in the profile" in (
        calendar_proposal_description
    )
    assert 'use ``target_scope="series"``' in calendar_proposal_description
    assert "never pass an occurrence ID" in calendar_proposal_description
    assert "durably queues the authorized provider operation" in (calendar_proposal_description)
    assert "conflict-resolution card" in calendar_proposal_description
    calendar_proposal_properties = calendar_proposal.inputSchema["properties"]
    assert calendar_proposal_properties["request_key"]["pattern"].startswith("^discord:")
    proposal_definition = calendar_proposal_properties["proposal"]
    assert proposal_definition["discriminator"]["propertyName"] == "kind"
    proposal_definitions = calendar_proposal.inputSchema["$defs"]
    for definition_name in (
        "UpdateCalendarEventProposal",
        "UpdateCalendarRemindersProposal",
        "CancelCalendarEventProposal",
    ):
        assert proposal_definitions[definition_name]["properties"]["target_scope"] == {
            "default": "event",
            "enum": ["event", "series"],
            "title": "Target Scope",
            "type": "string",
        }

    restore = tools["docket_restore_record"]
    restore_description = " ".join((restore.description or "").split())
    assert "Reactivate one archived canonical identity" in restore_description
    assert "does not itself recreate Google Calendar series" in restore_description
    assert "docket_apply_course_intent" in restore_description

    course_proposal = tools["docket_apply_course_intent"]
    course_description = " ".join((course_proposal.description or "").split())
    assert "one independent course record" in course_description
    assert "create, update, cancel, and no-op effects" in course_description
    assert "archives the course only after every linked active series" in course_description
    assert "Omitted courses are never inferred as drops" in course_description
    course_properties = course_proposal.inputSchema["properties"]
    assert course_properties["mode"]["enum"] == ["sync", "drop"]
    assert course_properties["request_key"]["pattern"].startswith("^discord:")
    assert "reason" not in course_proposal.inputSchema["required"]

    set_profile = tools["docket_set_calendar_profile"]
    set_profile_description = " ".join((set_profile.description or "").split())
    assert "Removing ``docket_queue`` disables existing Docket reminder rules" in (
        set_profile_description
    )
    profile_definition = set_profile.inputSchema["$defs"]["CalendarProfileInput"]
    assert profile_definition["properties"]["proposal_mode"]["enum"] == [
        "explicit_only",
        "suggest",
        "off",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_triage_surface_is_separate_and_strictly_bounded() -> None:
    interactive_names = {tool.name for tool in await mcp.list_tools()}
    tools = {tool.name: tool for tool in await triage_mcp.list_tools()}
    names = set(tools)
    assert names == {
        "docket_claim_triage_batch",
        "docket_read_claimed_source",
        "docket_search_related_records",
        "docket_submit_semantic_candidates",
    }
    assert names.isdisjoint(interactive_names)
    assert not any(
        forbidden in name
        for name in names
        for forbidden in (
            "approve",
            "execute",
            "calendar",
            "discord",
            "gmail_modify",
            "record_update",
            "terminal",
        )
    )
    template = Path("hermes/triage-config.example.yaml").read_text(encoding="utf-8")
    include_block = template.split("    tools:\n      include:\n", 1)[1].split(
        "      prompts:",
        1,
    )[0]
    configured_names = {
        line.removeprefix("        - ").strip()
        for line in include_block.splitlines()
        if line.strip()
    }
    assert configured_names == names
    assert "cli: []" in template
    assert "cron: []" in template
    assert "discord:" not in template
    assert "plugins:\n  enabled: []" in template
    assert "/triage-mcp/" in template
    read_description = " ".join((tools["docket_read_claimed_source"].description or "").split())
    assert "explicitly untrusted data" in read_description
    assert "never stored" in read_description
    submit = tools["docket_submit_semantic_candidates"]
    submit_description = " ".join((submit.description or "").split())
    assert "never authorize Gmail housekeeping" in submit_description
    candidate = submit.inputSchema["$defs"]["SemanticCandidateInput"]
    assert candidate["properties"]["kind"]["enum"] == [
        "event",
        "deadline",
        "response",
        "task",
        "information",
        "noise",
    ]
    assert candidate["properties"]["mutation"]["enum"] == [
        "create",
        "update",
        "cancel",
        "none",
    ]
    assert "action_types" not in submit.inputSchema["properties"]

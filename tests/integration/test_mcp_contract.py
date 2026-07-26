import runpy
from pathlib import Path

import pytest

from docket.mcp import mcp, triage_mcp


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_tools_and_active_template_allowlist_move_together() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert names == {
        "docket_store_record",
        "docket_get_record",
        "docket_search_records",
        "docket_update_record",
        "docket_archive_record",
        "docket_restore_record",
        "docket_list_accounts",
        "docket_list_calendar_events",
        "docket_get_calendar_sync_status",
        "docket_get_calendar_profile",
        "docket_set_calendar_profile",
        "docket_list_reminder_rules",
        "docket_propose_calendar_event",
        "docket_propose_course_reconciliation",
        "docket_list_queue_items",
        "docket_get_queue_item",
        "docket_snooze_queue_item",
        "docket_ignore_queue_item",
        "docket_get_action",
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
    smoke_contract = runpy.run_path("scripts/compose-mcp-smoke.py")
    assert smoke_contract["EXPECTED_TOOLS"] == names
    store_description = " ".join((tools["docket_store_record"].description or "").split())
    assert "not Hermes memory" in store_description
    assert "even when search found" in store_description
    assert "attaching the current source provenance" in store_description
    assert "record_conflict" in store_description
    assert "Never copy the existing record" in store_description
    assert "docket_update_record" in store_description
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
    assert "07:00 Los Angeles rollover" in snooze_description
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

    calendar_proposal_definitions = tools["docket_propose_calendar_event"].inputSchema[
        "$defs"
    ]
    calendar_proposal_description = " ".join(
        (tools["docket_propose_calendar_event"].description or "").split()
    )
    assert "inherits Docket's configured ``DOCKET_TIMEZONE``" in (
        calendar_proposal_description
    )
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

    calendar_proposal = tools["docket_propose_calendar_event"]
    calendar_proposal_description = " ".join((calendar_proposal.description or "").split())
    assert "create, update, reminder change, or cancellation" in (calendar_proposal_description)
    assert "both Google popup and Docket's due-date ISO queue thread" in (
        calendar_proposal_description
    )
    assert 'use ``target_scope="series"``' in calendar_proposal_description
    assert "never pass an occurrence ID" in calendar_proposal_description
    assert "never mutates Google Calendar" in calendar_proposal_description
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
    assert "docket_propose_course_reconciliation" in restore_description

    course_proposal = tools["docket_propose_course_reconciliation"]
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
    assert "cannot split Google and Docket delivery" in set_profile_description
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
        "docket_submit_triage_decision",
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
    template = Path("hermes/triage-config.example.yaml").read_text(
        encoding="utf-8"
    )
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
    assert "/triage-mcp/" in template
    read_description = " ".join(
        (tools["docket_read_claimed_source"].description or "").split()
    )
    assert "explicitly untrusted data" in read_description
    assert "never stored" in read_description

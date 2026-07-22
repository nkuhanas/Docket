import runpy
from pathlib import Path

import pytest

from docket.mcp import mcp


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
        "docket_list_accounts",
        "docket_list_calendar_events",
        "docket_get_calendar_sync_status",
        "docket_list_reminder_rules",
        "docket_set_reminder_rule",
        "docket_disable_reminder_rule",
        "docket_list_queue_items",
        "docket_get_queue_item",
        "docket_snooze_queue_item",
        "docket_ignore_queue_item",
        "docket_propose_action",
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

    proposal = tools["docket_propose_action"]
    proposal_description = " ".join((proposal.description or "").split())
    assert "never records or consumes an approval" in proposal_description
    assert "never contacts Google Calendar" in proposal_description
    assert "persistent Approve/Reject buttons" in proposal_description
    assert "Do not instruct the operator to type an approval code" in proposal_description
    proposal_properties = proposal.inputSchema["properties"]
    assert proposal_properties["action_type"]["enum"] == [
        "calendar_create_meeting",
        "calendar_update_meeting",
    ]
    assert "risk_class" not in proposal_properties

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
    assert "never expose descriptions, attendees, conference data" in lookup_description
    lookup_properties = calendar_lookup.inputSchema["properties"]
    assert lookup_properties["freshness"]["enum"] == ["prefer_cache", "require_fresh"]
    assert lookup_properties["limit"]["maximum"] == 100

    list_rules = tools["docket_list_reminder_rules"]
    list_rules_description = " ".join((list_rules.description or "").split())
    assert "rather than conversational memory or a past-session search" in (
        list_rules_description
    )
    list_rules_properties = list_rules.inputSchema["properties"]
    assert list_rules_properties["limit"]["maximum"] == 100

    reminder = tools["docket_set_reminder_rule"]
    reminder_description = " ".join((reminder.description or "").split())
    assert "cannot send arbitrary immediate text" in reminder_description
    assert "never infers a standing rule" in reminder_description
    reminder_properties = reminder.inputSchema["properties"]
    assert reminder_properties["scope"]["enum"] == ["calendar", "event"]
    assert reminder_properties["request_key"]["pattern"].startswith("^discord:")

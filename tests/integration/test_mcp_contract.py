import pytest

from docket.mcp import mcp


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_and_milestone_two_scaffold_tools_are_exposed() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert names == {
        "docket_store_record",
        "docket_get_record",
        "docket_search_records",
        "docket_update_record",
        "docket_archive_record",
        "docket_list_accounts",
        "docket_propose_action",
        "docket_get_action",
    }
    assert not names.intersection(
        {"record_approval", "consume_approval", "execute_action", "raw_gmail_modify"}
    )
    store_description = " ".join(
        (tools["docket_store_record"].description or "").split()
    )
    assert "not Hermes memory" in store_description
    assert "even when search found" in store_description
    assert "attaching the current source provenance" in store_description
    assert "record_conflict" in store_description
    assert "Never copy the existing record" in store_description
    assert "docket_update_record" in store_description
    search_description = " ".join(
        (tools["docket_search_records"].description or "").split()
    )
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

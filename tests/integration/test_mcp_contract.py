import pytest

from docket.mcp import mcp


@pytest.mark.integration
@pytest.mark.asyncio
async def test_only_milestone_one_tools_are_exposed() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = set(tools)
    assert names == {
        "docket_remember_record",
        "docket_get_record",
        "docket_search_records",
        "docket_update_record",
        "docket_archive_record",
    }
    assert not names.intersection(
        {"record_approval", "consume_approval", "execute_action", "raw_gmail_modify"}
    )
    remember_description = " ".join(
        (tools["docket_remember_record"].description or "").split()
    )
    assert "not Hermes memory" in remember_description
    assert "even when search found" in remember_description
    assert "attaching the current source provenance" in remember_description
    search_description = " ".join(
        (tools["docket_search_records"].description or "").split()
    )
    assert "before answering operational facts" in search_description
    assert "Never claim a remember/store request succeeded" in search_description

    remember_schema = tools["docket_remember_record"].inputSchema
    properties = remember_schema["properties"]
    definitions = remember_schema["$defs"]
    assert properties["record_type"]["enum"] == ["term", "generic"]
    assert properties["request_key"]["pattern"].startswith("^discord:")
    assert properties["actor_id"]["pattern"] == "^[0-9]{17,20}$"
    assert definitions["TermData"]["additionalProperties"] is False
    assert definitions["TermData"]["required"] == ["institution", "term_name"]
    assert definitions["RecordSourceInput"]["properties"]["source_type"]["const"] == (
        "discord_message"
    )

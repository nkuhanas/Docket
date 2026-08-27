import pytest

from docket.domain.errors import IdempotencyConflict
from docket.mcp import server
from docket.schemas.records import DiscordSourceMetadata, RecordSourceInput


def test_entity_write_consumes_request_key_once(session, monkeypatch) -> None:
    monkeypatch.setattr(server, "_validate_entity_write", lambda *args, **kwargs: None)
    source = RecordSourceInput(
        source_type="discord_message",
        source_object_id="000000000000000006",
        metadata=DiscordSourceMetadata(
            guild_id="000000000000000002",
            channel_id="000000000000000003",
            message_id="000000000000000006",
            user_id="000000000000000001",
            intent_index=0,
        ),
    )
    request_key = (
        "discord:000000000000000002:000000000000000003:000000000000000006:0"
    )
    executions = 0

    def execute() -> dict[str, object]:
        nonlocal executions
        executions += 1
        return {"ok": True, "entity": {"entity_id": "example"}}

    first = server._execute_entity_write(
        session,
        request_key=request_key,
        source=source,
        actor_id="000000000000000001",
        operation_name="docket_create_entity",
        payload={"canonical_name": "Example"},
        execute=execute,
    )
    replay = server._execute_entity_write(
        session,
        request_key=request_key,
        source=source,
        actor_id="000000000000000001",
        operation_name="docket_create_entity",
        payload={"canonical_name": "Example"},
        execute=execute,
    )

    assert first == {"ok": True, "entity": {"entity_id": "example"}}
    assert replay == {**first, "disposition": "replayed_request"}
    assert executions == 1

    with pytest.raises(IdempotencyConflict):
        server._execute_entity_write(
            session,
            request_key=request_key,
            source=source,
            actor_id="000000000000000001",
            operation_name="docket_create_entity",
            payload={"canonical_name": "Different"},
            execute=execute,
        )

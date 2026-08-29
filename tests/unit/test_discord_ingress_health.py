from pathlib import Path

import pytest

import docket.discord_ingress as discord_ingress
from docket.discord_ingress import StableDiscordIngress


@pytest.mark.asyncio
async def test_gateway_resume_restores_ingress_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "docket-ingress-ready"
    monkeypatch.setattr(discord_ingress, "_READY_PATH", ready_path)
    client = object.__new__(StableDiscordIngress)

    await client.on_resumed()
    assert ready_path.is_file()

    await client.on_disconnect()
    assert not ready_path.exists()

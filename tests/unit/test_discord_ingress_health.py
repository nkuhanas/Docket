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


@pytest.mark.asyncio
async def test_attachment_download_prefers_fresh_discord_url() -> None:
    calls: list[bool] = []

    class Attachment:
        async def read(self, *, use_cached: bool) -> bytes:
            calls.append(use_cached)
            return b"pdf"

    assert await discord_ingress._read_attachment_bytes(Attachment()) == b"pdf"
    assert calls == [False]


@pytest.mark.asyncio
async def test_attachment_download_falls_back_to_cached_proxy() -> None:
    calls: list[bool] = []

    class Attachment:
        async def read(self, *, use_cached: bool) -> bytes:
            calls.append(use_cached)
            if not use_cached:
                raise RuntimeError("fresh URL failed")
            return b"pdf"

    assert await discord_ingress._read_attachment_bytes(Attachment()) == b"pdf"
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_attachment_download_returns_none_after_both_paths_fail() -> None:
    calls: list[bool] = []

    class Attachment:
        async def read(self, *, use_cached: bool) -> bytes:
            calls.append(use_cached)
            raise RuntimeError("download failed")

    assert await discord_ingress._read_attachment_bytes(Attachment()) is None
    assert calls == [False, True]

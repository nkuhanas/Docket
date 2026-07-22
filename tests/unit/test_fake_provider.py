import pytest

from docket.providers.google import FakeGoogleProvider


def test_fake_provider_never_mutates_network() -> None:
    provider = FakeGoogleProvider()
    assert provider.smoke_status()["external_calls"] is False
    with pytest.raises(RuntimeError, match="cannot perform external mutations"):
        provider.create_calendar_event()

from typing import Any


class FakeGoogleProvider:
    """Non-networking provider used by smoke and automated tests."""

    def smoke_status(self) -> dict[str, Any]:
        return {
            "provider": "fake-google",
            "external_calls": False,
            "gmail": "simulated",
            "calendar": "simulated",
        }

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("create", "update", "archive", "mark", "send", "delete")):
            raise RuntimeError("FakeGoogleProvider cannot perform external mutations")
        raise AttributeError(name)

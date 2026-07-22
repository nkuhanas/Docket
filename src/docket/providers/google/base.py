from typing import Any, Protocol


class GoogleProvider(Protocol):
    def smoke_status(self) -> dict[str, Any]: ...

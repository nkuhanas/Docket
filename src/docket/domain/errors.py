from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DocketError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details or {},
            },
        }


class IdempotencyConflict(DocketError):
    def __init__(
        self,
        request_key: str,
        *,
        existing_operation: str | None = None,
        attempted_operation: str | None = None,
    ) -> None:
        details = {"request_key": request_key}
        if existing_operation is not None:
            details["existing_operation"] = existing_operation
        if attempted_operation is not None:
            details["attempted_operation"] = attempted_operation
        super().__init__(
            code="idempotency_conflict",
            message="The request key was already used with different input.",
            details=details,
        )

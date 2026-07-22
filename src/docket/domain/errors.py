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
    def __init__(self, request_key: str) -> None:
        super().__init__(
            code="idempotency_conflict",
            message="The request key was already used with different input.",
            details={"request_key": request_key},
        )


class RecordNotFound(DocketError):
    def __init__(self, record_id: str) -> None:
        super().__init__(
            code="record_not_found",
            message="The requested record does not exist.",
            details={"record_id": record_id},
        )


class VersionConflict(DocketError):
    def __init__(self, record_id: str, expected: int, current: int) -> None:
        super().__init__(
            code="version_conflict",
            message="The record changed after it was read.",
            details={
                "record_id": record_id,
                "expected_version": expected,
                "current_version": current,
            },
        )


class ActionDisabled(DocketError):
    def __init__(self, action_type: str) -> None:
        super().__init__(
            code="action_disabled",
            message="This action is disabled in the current release.",
            details={"action_type": action_type},
        )

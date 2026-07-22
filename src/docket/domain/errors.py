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


class RecordConflict(DocketError):
    def __init__(
        self,
        record_id: str,
        version: int,
        differing_fields: list[str],
    ) -> None:
        super().__init__(
            code="record_conflict",
            message=(
                "The canonical identity already exists with different data; "
                "no source provenance was attached. Do not copy the existing "
                "record and retry as though the current source asserted it; "
                "request an explicit update decision."
            ),
            details={
                "record_id": record_id,
                "current_version": version,
                "differing_fields": differing_fields,
            },
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

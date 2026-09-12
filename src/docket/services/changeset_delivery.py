"""Bounded read-only delivery status for one committed request.

Read committed provider snapshots, never today's possibly edited canonical
objects. A status read does not stage, retry, or authorize an external effect.
"""

from __future__ import annotations

import base64
import json
from collections import Counter
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import case, func, select, true
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import ChangeSet, Operation, OperationTarget
from docket.models.base import utc_now
from docket.schemas.calendar import CalendarEventTiming

_STATES = {
    "pending": "queued", "running": "executing", "succeeded": "confirmed",
    "failed": "failed", "partial_failed": "partial_failed",
    "reconciliation_required": "reconciling",
}


def _next_action(state: str, error: str | None) -> str:
    if state == "confirmed":
        return "none"
    if state == "reconciling":
        return "await_reconciliation_do_not_resend"
    if state in {"queued", "executing"}:
        return "follow_operation_status"
    if error == "google_auth_invalid":
        return "restore_provider_authorization_then_recover_same_operation"
    return "inspect_failed_operation_do_not_restage_request"


class ChangeSetDeliveryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def read(
        self, changeset: ChangeSet, *, cursor: str | None = None, limit: int = 25
    ) -> dict[str, Any]:
        if changeset.state != "committed":
            raise DocketError(
                code="changeset_not_committed",
                message="There is no committed delivery receipt; continue the existing draft.",
            )
        limit = min(max(limit, 1), 100)
        position = 0
        if cursor is not None:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                position = value["position"]
                if (
                    type(value["version"]) is not int or value["version"] != 1
                    or value["changeset_ref"] != changeset.ref_id
                    or type(position) is not int or not 0 <= position <= 1_000_000
                ):
                    raise ValueError("invalid cursor")
            except (ValueError, TypeError, KeyError) as exc:
                raise DocketError(
                    code="invalid_delivery_cursor", message="Restart this ChangeSet delivery read."
                ) from exc
        # Count Operations, not joined targets. The single SQL statement gives
        # page rows and exact whole-request counts one database read snapshot.
        operations = select(Operation).where(
            Operation.originating_changeset_ref == changeset.ref_id
        ).cte("delivery_operations")
        totals = select(
            func.count().label("operation_count"),
            *[
                func.coalesce(func.sum(case((operations.c.status == state, 1), else_=0)), 0)
                .label(f"count_{state}")
                for state in _STATES
            ],
        ).select_from(operations).cte("delivery_totals")
        targets = select(
            operations.c.ref_id.label("operation_ref"),
            operations.c.operation_type,
            operations.c.status.label("operation_status"),
            operations.c.last_error_code.label("operation_error"),
            OperationTarget.canonical_target_ref.label("target_ref"),
            OperationTarget.target_key,
            OperationTarget.status.label("target_status"),
            OperationTarget.last_error_code.label("target_error"),
            OperationTarget.parameters,
            func.count().over().label("target_count"),
        ).select_from(operations).outerjoin(
            OperationTarget, OperationTarget.operation_id == operations.c.id
        ).cte("delivery_targets")
        page_query = select(targets).order_by(
            targets.c.operation_ref, targets.c.target_key
        ).offset(position).limit(limit + 1).cte("delivery_page")
        rows = self.session.execute(
            select(totals, page_query).select_from(totals).outerjoin(page_query, true())
            .order_by(page_query.c.operation_ref, page_query.c.target_key)
        ).mappings().all()
        first = rows[0]
        counts = {
            display: int(first[f"count_{state}"]) for state, display in _STATES.items()
            if first[f"count_{state}"]
        }
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            if row["operation_ref"] is None:
                continue
            parameters = row["parameters"] or {}
            event = parameters.get("event") or {}
            title = event.get("title") or parameters.get("display_name")
            timing: dict[str, Any] | None = None
            timing_invalid = False
            if event.get("timing") is not None:
                try:
                    timing = TypeAdapter(CalendarEventTiming).validate_python(
                        event["timing"]
                    ).model_dump(mode="json", exclude_none=True)
                except ValidationError:
                    timing_invalid = True
            state = _STATES.get(row["target_status"], "unknown")
            error = row["target_error"] or row["operation_error"]
            detail = {
                "operation_ref": row["operation_ref"],
                "operation_type": row["operation_type"],
                "target_ref": row["target_ref"],
                "delivery_state": state,
                "operation_state": _STATES.get(row["operation_status"], "unknown"),
                "title": str(title)[:512] if title is not None else None,
                "title_truncated": len(str(title)) > 512 if title is not None else False,
                "timing": timing,
                "snapshot_diagnostic": "invalid_stored_timing" if timing_invalid else None,
                "lane_ref": parameters.get("lane_ref"),
                "error_code": error if state != "confirmed" else None,
                "next_action": _next_action(state, error),
            }
            detail = {key: value for key, value in detail.items() if value is not None}
            candidate = [*items, detail]
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > 8_000:
                break
            items = candidate
        total_targets = int(first["target_count"] or 0)
        if position and not items:
            raise DocketError(
                code="invalid_delivery_cursor", message="Delivery cursor exceeds this request."
            )
        next_position = position + len(items)
        next_cursor = None
        if next_position < total_targets:
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "version": 1, "changeset_ref": changeset.ref_id, "position": next_position,
            }, separators=(",", ":")).encode()).decode().rstrip("=")
        operation_count = int(first["operation_count"])
        disposition = (
            "no_provider_operations" if operation_count == 0
            else "confirmed" if counts.get("confirmed") == operation_count
            else "needs_recovery" if counts.get("failed") or counts.get("partial_failed")
            else "reconciling" if counts.get("reconciling")
            else "in_progress"
        )
        return {
            "ok": True, "ref": changeset.ref_id, "canonical_disposition": "committed",
            "observed_at": utc_now().isoformat(), "status_semantics": "live_read_snapshot",
            "provider_operation_count": operation_count,
            "provider_disposition": disposition,
            "provider_state_counts": counts,
            "delivery_target_count": total_targets, "items": items, "count": len(items),
            "page_state_counts": dict(Counter(str(item["delivery_state"]) for item in items)),
            "omitted_target_count": max(total_targets - next_position, 0),
            "truncated": next_cursor is not None,
            **({"cursor": next_cursor} if next_cursor else {}),
            "next": {
                "action": "none" if disposition in {"confirmed", "no_provider_operations"}
                else "follow_existing_operations",
                "restage_request": False,
            },
        }

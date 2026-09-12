"""Bounded trace projections; wrapper attempts are not authenticated invocations."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import ConversationalToolTrace, ToolInvocation
from docket.models.base import utc_now
from docket.services.trace_correlation import correlated_calls


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _milliseconds(start: datetime, end: datetime) -> int:
    return max(0, int((_utc(end) - _utc(start)).total_seconds() * 1000))


def _interval_union(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end = 0
    for left, right in sorted(intervals):
        if right > end:
            total += right - max(left, end)
            end = right
    return total


class TraceViewService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self, trace: ConversationalToolTrace) -> dict[str, Any]:
        invocations = list(
            self.session.scalars(
                select(ToolInvocation).where(ToolInvocation.trace_ref == trace.ref_id)
            )
        )
        by_call = correlated_calls(invocations)
        as_of = utc_now()
        total_ms = _milliseconds(trace.started_at, trace.completed_at or as_of)
        intervals = []
        for item in invocations:
            # A running/uncertain invocation is not proof of uninterrupted CPU,
            # provider waiting or even a live process. Only closed intervals are
            # measured here; pending evidence remains explicitly unmeasured.
            if item.completed_at is not None:
                left = min(total_ms, _milliseconds(trace.started_at, item.started_at))
                right = min(total_ms, _milliseconds(trace.started_at, item.completed_at))
                if right >= left:
                    intervals.append((left, right))
        execution_ms = _interval_union(intervals)
        first = min((item.started_at for item in invocations), default=None)
        rows: list[dict[str, Any]] = []
        wrapper_call_ids = {str(call.get("call_id", "")) for call in trace.calls}
        for call in sorted(trace.calls, key=lambda row: int(row["ordinal"])):
            invocation = by_call.get(str(call.get("call_id", "")))
            if invocation is not None and (
                invocation.tool_name != call["tool_name"]
                or invocation.trace_ordinal != call["ordinal"]
                or invocation.received_argument_hash != call.get("received_argument_hash")
            ):
                invocation = None
            local = call.get("execution_boundary") == "local_rejection"
            origin = (
                "authenticated_docket"
                if invocation
                else ("local_rejection" if local else "unreconciled")
            )
            disposition = (
                invocation.result_disposition
                if invocation
                else (call.get("disposition") if local else None)
            )
            rows.append(
                {
                    "ordinal": int(call["ordinal"]),
                    "tool_name": str(call["tool_name"])[:128],
                    "origin": origin,
                    "transport_state": str(call.get("transport_state", "running")),
                    "transport_layer": "wrapper",
                    "domain_state": invocation.domain_state if invocation else "unknown",
                    "outcome": str(disposition or "unknown")[:128],
                    "transport_error_code": str(call.get("transport_error_code") or "none")[:64],
                    "elapsed_ms": min(max(int(call.get("elapsed_ms", 0)), 0), 600_000),
                    "tool_call_ref": invocation.ref_id if invocation else "unreconciled",
                    "argument_preview": str(call.get("argument_preview", "{}"))[:768],
                }
            )
        for call_id, invocation in by_call.items():
            if call_id in wrapper_call_ids or invocation.trace_ordinal is None:
                continue
            # The signed invocation establishes this upstream attempt even if
            # every wrapper callback was lost. It does not establish wrapper
            # latency or whether Hermes received the response.
            rows.append({
                "ordinal": invocation.trace_ordinal, "tool_name": invocation.tool_name,
                "origin": "authenticated_docket", "transport_state": invocation.transport_state,
                "transport_layer": "docket",
                "domain_state": invocation.domain_state,
                "outcome": invocation.result_disposition or "unknown",
                "transport_error_code": "none", "elapsed_ms": None,
                "tool_call_ref": invocation.ref_id,
                "argument_preview": '{"availability":"not_recorded"}',
            })
        rows.sort(key=lambda row: int(row["ordinal"]))
        origins = Counter(row["origin"] for row in rows)
        tools = Counter(row["tool_name"] for row in rows)
        confirmed_tools = Counter(item.tool_name for item in invocations)
        return {
            "trace_ref": trace.ref_id,
            "trace_version": trace.version,
            "status": trace.status,
            "as_of": _utc(as_of).isoformat(),
            "counts": {
                "attempts": len(rows),
                "authenticated_invocations": len(invocations),
                "local_rejections": origins["local_rejection"],
                "unreconciled_attempts": origins["unreconciled"],
                "unfinished_invocations": sum(item.completed_at is None for item in invocations),
            },
            "tool_counts": [
                {
                    "tool_name": name,
                    "attempts": tools[name],
                    "authenticated_invocations": confirmed_tools[name],
                }
                for name in sorted(tools.keys() | confirmed_tools.keys())
            ],
            "timing": {
                "total_elapsed_ms": total_ms,
                "before_first_docket_call_ms": (
                    min(total_ms, _milliseconds(trace.started_at, first)) if first else None
                ),
                "docket_execution_ms": execution_ms,
                # This aggregate can overlap both itself and Docket intervals.
                # It is deliberately NOT subtracted from wall-clock elapsed.
                "wrapper_elapsed_sum_ms": sum(row["elapsed_ms"] or 0 for row in rows),
                "unattributed_ms": total_ms - execution_ms,
                "queue_ms": None,
                "context_schema_ms": None,
                "model_ms": None,
                "local_validation_ms": None,
                "provider_wait_ms": None,
            },
            "rows": rows,
        }

    def read(
        self, trace: ConversationalToolTrace, *, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        view = self.snapshot(trace)
        snapshot_hash = sha256_json(view["rows"])
        position = 0
        if cursor is not None:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not isinstance(payload, dict) or set(payload) != {
                    "format",
                    "trace_ref",
                    "trace_version",
                    "snapshot_hash",
                    "position",
                }:
                    raise ValueError
                if (
                    payload["format"] != 2
                    or type(payload["format"]) is not int
                    or payload["trace_ref"] != trace.ref_id
                    or type(payload["trace_version"]) is not int
                    or type(payload["position"]) is not int
                    or payload["position"] < 0
                ):
                    raise ValueError
                if payload["trace_version"] != trace.version or (
                    payload["snapshot_hash"] != snapshot_hash
                ):
                    raise DocketError(
                        code="trace_revision_changed",
                        message="The trace advanced; restart its bounded call read.",
                        details={"next_action": "restart_trace_read"},
                    )
                position = payload["position"]
            except (ValueError, TypeError, KeyError) as exc:
                raise DocketError(
                    code="invalid_trace_cursor", message="Restart the trace call read."
                ) from exc
        rows = view.pop("rows")
        if position > len(rows):
            raise DocketError(code="invalid_trace_cursor", message="Cursor exceeds this trace.")
        page: list[dict[str, Any]] = []
        for row in rows[position : position + min(max(limit, 1), 100)]:
            if len(json.dumps([*page, row], ensure_ascii=False).encode()) > 8_000:
                break
            page.append(row)
        end = position + len(page)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "format": 2,
                        "trace_ref": trace.ref_id,
                        "trace_version": trace.version,
                        "snapshot_hash": snapshot_hash,
                        "position": end,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        return {
            "ok": True,
            "ref": trace.ref_id,
            "object_type": "conversational_tool_trace",
            **view,
            "items": page,
            "count": len(page),
            "total_if_known": len(rows),
            "omitted_detail_count": len(rows) - end,
            "truncated": end < len(rows),
            **({"cursor": next_cursor} if end < len(rows) else {}),
            "timing_scope": "trace_window_closed_docket_intervals_only",
        }

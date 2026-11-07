"""Lossless Task exports through bounded, connection-owned read snapshots."""

import base64
import csv
import io
import json
from typing import Any, Literal

from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.errors import DocketError
from docket.models import Task
from docket.services.native_context import NativeContextService
from docket.services.task_views import iso

FIELDS = (
    "ref", "item_ref", "title", "notes", "state", "priority", "canonical_status",
    "task_list_ref", "parent_task_ref", "scheduled_date", "remind_at", "timezone", "version",
)


def _chunks(content: str) -> list[str]:
    # Bound escaped wire bytes too: quotes, newlines and non-ASCII can expand.
    result = []
    offset = 0
    while offset < len(content):
        low, high = 1, min(len(content) - offset, 8000)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(content[offset:offset + middle]).encode()) <= 8000:
                low = middle
            else:
                high = middle - 1
        result.append(content[offset:offset + low])
        offset += low
    return result or [""]


class TaskExportService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.contexts = NativeContextService(session, settings)

    def continue_chunk(self, ref: str, identity: str) -> dict[str, Any]:
        retained = self.contexts.require(ref, "task-export")
        if retained.snapshot["query_hash"] != identity:
            raise DocketError(code="task_cursor_invalid", message="Keep the same query filters.")
        return self._packet(retained.ref_id, retained.snapshot)

    @staticmethod
    def _packet(ref: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        cursor = snapshot["next_page"]
        if snapshot["next_chunk"]:
            cursor = base64.urlsafe_b64encode(json.dumps({
                "filter": snapshot["query_hash"], "export_ref": snapshot["next_chunk"],
            }, separators=(",", ":")).encode()).decode()
        return {
            "ok": True, "context_id": ref, "format": snapshot["format"],
            "content_type": "application/json" if snapshot["format"] == "json" else "text/csv",
            "content": snapshot["content"], "total_matching": snapshot["total_matching"],
            "observed_at": snapshot["observed_at"], "next_cursor": cursor,
            "coverage": {"source": "canonical_tasks", "google_refreshed": False,
                         "snapshot": "captured page; later pages are read at continuation"},
        }

    def page(
        self, tasks: list[Task], format: Literal["json", "csv"], identity: str, total: int,
        *, first: bool, next_page: str | None,
    ) -> dict[str, Any]:
        rows = [{
            "ref": task.ref_id, "item_ref": task.item_ref, "title": task.title,
            "notes": task.description, "state": task.task_state, "priority": task.priority,
            "canonical_status": task.canonical_status, "task_list_ref": task.task_list_ref,
            "parent_task_ref": task.parent_task_ref,
            "scheduled_date": task.scheduled_date.isoformat() if task.scheduled_date else None,
            "remind_at": iso(task.remind_at), "timezone": task.reminder_timezone,
            "version": task.version,
        } for task in tasks]
        if format == "json":
            content = ("[" if first else "," if rows else "")
            content += ",".join(json.dumps(row, ensure_ascii=False) for row in rows)
            if next_page is None:
                content += "]"
        else:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
            if first:
                writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue()
        next_chunk: str | None = None
        packet: dict[str, Any] = {}
        for chunk in reversed(_chunks(content)):
            snapshot = {
                "format": format, "query_hash": identity, "content": chunk,
                "total_matching": total, "next_chunk": next_chunk, "next_page": next_page,
            }
            retained = self.contexts.retain("task-export", snapshot, {})
            next_chunk = retained["context_id"]
            packet = self._packet(next_chunk, retained)
        return packet

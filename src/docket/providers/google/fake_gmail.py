from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from docket.providers.google.gmail import (
    GmailClaimedContent,
    GmailCursorInvalid,
    GmailMessageMetadata,
    GmailScanPage,
)


class FakeGmailProvider:
    """Deterministic fake for cursor, overlap, claim, and mutation tests."""

    def __init__(self, *, page_size: int = 100) -> None:
        self.page_size = page_size
        self.messages: dict[str, GmailClaimedContent] = {}
        self.received_at: dict[str, datetime] = {}
        self.history: list[str] = []
        self.invalidate_next_history_cursor = False
        self.scan_calls = 0
        self.read_calls = 0

    def add_message(
        self,
        *,
        message_id: str,
        thread_id: str | None = None,
        source_version: str | None = None,
        sender: str | None = None,
        subject: str | None = None,
        label_ids: tuple[str, ...] = ("INBOX", "UNREAD"),
        body_text: str = "",
        attachments: tuple[dict[str, Any], ...] = (),
        received_at: datetime | None = None,
    ) -> None:
        version = source_version or str(len(self.history) + 1)
        self.messages[message_id] = GmailClaimedContent(
            message_id=message_id,
            thread_id=thread_id,
            source_version=version,
            sender=sender,
            subject=subject,
            label_ids=tuple(sorted(label_ids)),
            body_text=body_text,
            attachments=attachments,
        )
        self.received_at[message_id] = received_at or datetime.now(UTC)
        self.history.append(message_id)

    def _metadata(self, message_id: str) -> GmailMessageMetadata:
        message = self.messages[message_id]
        return GmailMessageMetadata(
            message_id=message.message_id,
            thread_id=message.thread_id,
            source_version=message.source_version,
            received_at=self.received_at[message_id],
            sender=message.sender,
            subject=message.subject,
            label_ids=message.label_ids,
            size_estimate=len(message.body_text.encode()),
        )

    def scan_page(
        self,
        *,
        cursor: dict[str, Any],
        recovery_after: datetime,
    ) -> GmailScanPage:
        self.scan_calls += 1
        mode = str(cursor.get("mode") or "recovery")
        if mode == "history" and self.invalidate_next_history_cursor:
            self.invalidate_next_history_cursor = False
            raise GmailCursorInvalid()
        offset = int(cursor.get("page_token") or 0)
        if mode == "history":
            start = int(cursor.get("start_history_id") or cursor.get("history_id") or 0)
            candidates = self.history[start:]
            recovery = False
        else:
            candidates = [
                message_id
                for message_id in self.history
                if self.received_at[message_id] >= recovery_after
            ]
            recovery = True
        page_ids = candidates[offset : offset + self.page_size]
        next_offset = offset + len(page_ids)
        has_next = next_offset < len(candidates)
        target = str(len(self.history))
        if has_next:
            next_cursor: dict[str, Any] = {
                "mode": mode,
                "page_token": str(next_offset),
                "start_history_id": str(cursor.get("history_id") or 0),
                "target_history_id": target,
            }
            if mode == "recovery":
                next_cursor["after_epoch"] = int(recovery_after.timestamp())
                next_cursor["anchor_history_id"] = target
        else:
            next_cursor = {"mode": "history", "history_id": target}
        return GmailScanPage(
            messages=tuple(self._metadata(message_id) for message_id in page_ids),
            next_cursor=next_cursor,
            observed_through=datetime.now(UTC),
            provider_request_id=f"fake-gmail-scan-{self.scan_calls}",
            recovery=recovery,
        )

    def read_message(self, message_id: str) -> GmailClaimedContent:
        self.read_calls += 1
        return self.messages[message_id]

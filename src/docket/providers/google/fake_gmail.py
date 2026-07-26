from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from docket.providers.google.gmail import (
    GmailClaimedContent,
    GmailCursorInvalid,
    GmailMessageMetadata,
    GmailMutationRequest,
    GmailMutationResult,
    GmailProviderError,
    GmailScanPage,
    GmailUnknownOutcome,
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
        self.mutation_calls = 0
        self.label_state_calls = 0
        self.transient_before_write_once = False
        self.permanent_before_write_once = False
        self.unknown_after_write_once = False
        self.transient_label_state_once = False
        self.permanent_label_state_once = False

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

    @staticmethod
    def _next_version(value: str) -> str:
        try:
            return str(int(value) + 1)
        except ValueError:
            return f"{value}.1"

    def get_label_state(self, request: GmailMutationRequest) -> GmailMutationResult:
        self.label_state_calls += 1
        if self.transient_label_state_once:
            self.transient_label_state_once = False
            raise GmailProviderError(
                "gmail_transient",
                "Fake Gmail was temporarily unavailable.",
                transient=True,
            )
        if self.permanent_label_state_once:
            self.permanent_label_state_once = False
            raise GmailProviderError(
                "google_auth_invalid",
                "Fake Gmail authorization is unavailable.",
                transient=False,
            )
        try:
            message = self.messages[request.message_id]
        except KeyError as exc:
            raise GmailProviderError(
                "gmail_message_not_found",
                "The Gmail message no longer exists.",
                transient=False,
            ) from exc
        return GmailMutationResult(
            message_id=message.message_id,
            source_version=message.source_version,
            label_ids=message.label_ids,
            provider_request_id=f"fake-gmail-read-{self.label_state_calls}",
            disposition="observed",
        )

    def mutate_message(self, request: GmailMutationRequest) -> GmailMutationResult:
        self.mutation_calls += 1
        if self.transient_before_write_once:
            self.transient_before_write_once = False
            raise GmailProviderError(
                "gmail_transient",
                "Fake Gmail was temporarily unavailable.",
                transient=True,
            )
        if self.permanent_before_write_once:
            self.permanent_before_write_once = False
            raise GmailProviderError(
                "gmail_rejected",
                "Fake Gmail rejected the mutation.",
                transient=False,
            )
        current = self.get_label_state(request)
        if request.remove_label_id not in current.label_ids:
            return GmailMutationResult(
                message_id=current.message_id,
                source_version=current.source_version,
                label_ids=current.label_ids,
                provider_request_id=f"fake-gmail-modify-{self.mutation_calls}",
                disposition="already_applied",
            )
        message = self.messages[request.message_id]
        labels = tuple(
            label for label in message.label_ids if label != request.remove_label_id
        )
        version = self._next_version(message.source_version)
        self.messages[request.message_id] = GmailClaimedContent(
            message_id=message.message_id,
            thread_id=message.thread_id,
            source_version=version,
            sender=message.sender,
            subject=message.subject,
            label_ids=labels,
            body_text=message.body_text,
            attachments=message.attachments,
        )
        if self.unknown_after_write_once:
            self.unknown_after_write_once = False
            raise GmailUnknownOutcome()
        return GmailMutationResult(
            message_id=message.message_id,
            source_version=version,
            label_ids=labels,
            provider_request_id=f"fake-gmail-modify-{self.mutation_calls}",
            disposition="modified",
        )

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from docket.providers.google.oauth import GMAIL_MODIFY_SCOPE

_MESSAGE_FIELDS = (
    "id,threadId,historyId,internalDate,labelIds,sizeEstimate,"
    "payload(headers(name,value),mimeType,filename,body(data,size,attachmentId),"
    "parts(headers(name,value),mimeType,filename,body(data,size,attachmentId),"
    "parts(headers(name,value),mimeType,filename,body(data,size,attachmentId))))"
)


class GmailProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.transient = transient


class GmailCursorInvalid(GmailProviderError):
    def __init__(self) -> None:
        super().__init__(
            "gmail_cursor_invalid",
            "The Gmail history cursor is no longer valid.",
            transient=False,
        )


class GmailUnknownOutcome(GmailProviderError):
    def __init__(self) -> None:
        super().__init__(
            "gmail_unknown_outcome",
            "Gmail did not confirm whether the label change was applied.",
            transient=False,
        )


@dataclass(frozen=True, slots=True)
class GmailMessageMetadata:
    message_id: str
    thread_id: str | None
    source_version: str
    received_at: datetime | None
    sender: str | None
    subject: str | None
    label_ids: tuple[str, ...]
    size_estimate: int | None


@dataclass(frozen=True, slots=True)
class GmailScanPage:
    messages: tuple[GmailMessageMetadata, ...]
    next_cursor: dict[str, Any]
    observed_through: datetime
    provider_request_id: str | None = None
    recovery: bool = False


@dataclass(frozen=True, slots=True)
class GmailClaimedContent:
    message_id: str
    thread_id: str | None
    source_version: str
    sender: str | None
    subject: str | None
    label_ids: tuple[str, ...]
    body_text: str
    attachments: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class GmailMutationRequest:
    message_id: str
    source_version: str
    remove_label_id: str
    provider_correlation: str


@dataclass(frozen=True, slots=True)
class GmailMutationResult:
    message_id: str
    source_version: str
    label_ids: tuple[str, ...]
    provider_request_id: str | None = None
    disposition: str = "modified"


class GmailReadProvider(Protocol):
    def scan_page(
        self,
        *,
        cursor: dict[str, Any],
        recovery_after: datetime,
    ) -> GmailScanPage: ...

    def read_message(self, message_id: str) -> GmailClaimedContent: ...


class GmailMutationProvider(Protocol):
    def mutate_message(self, request: GmailMutationRequest) -> GmailMutationResult: ...

    def get_label_state(self, request: GmailMutationRequest) -> GmailMutationResult: ...


def _normalized_sender(raw_value: object) -> str | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    display, address = parseaddr(raw_value)
    address = address.strip().casefold()
    if not address:
        return raw_value.strip()[:320]
    display = " ".join(display.split())[:160]
    return f"{display} <{address}>" if display else address


def _header(payload: object, name: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    for item in headers:
        if (
            isinstance(item, dict)
            and str(item.get("name", "")).casefold() == name.casefold()
            and isinstance(item.get("value"), str)
        ):
            return str(item["value"])
    return None


def _metadata(document: dict[str, Any]) -> GmailMessageMetadata:
    message_id = document.get("id")
    history_id = document.get("historyId")
    if not isinstance(message_id, str) or not message_id:
        raise GmailProviderError(
            "gmail_invalid_response",
            "Gmail returned a message without an ID.",
            transient=False,
        )
    if not isinstance(history_id, str) or not history_id:
        raise GmailProviderError(
            "gmail_invalid_response",
            "Gmail returned a message without a source version.",
            transient=False,
        )
    raw_internal_date = document.get("internalDate")
    received_at: datetime | None = None
    if isinstance(raw_internal_date, str):
        try:
            received_at = datetime.fromtimestamp(int(raw_internal_date) / 1000, UTC)
        except (ValueError, OverflowError):
            received_at = None
    payload = document.get("payload")
    raw_subject = _header(payload, "Subject")
    labels = document.get("labelIds")
    return GmailMessageMetadata(
        message_id=message_id,
        thread_id=(
            str(document["threadId"]) if isinstance(document.get("threadId"), str) else None
        ),
        source_version=history_id,
        received_at=received_at,
        sender=_normalized_sender(_header(payload, "From")),
        subject=(" ".join(raw_subject.split())[:500] if raw_subject else None),
        label_ids=tuple(
            sorted(str(value) for value in labels if isinstance(value, str))
            if isinstance(labels, list)
            else ()
        ),
        size_estimate=(
            int(document["sizeEstimate"])
            if isinstance(document.get("sizeEstimate"), int)
            else None
        ),
    )


def _decode_body(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        padded = value + ("=" * (-len(value) % 4))
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def _extract_content(payload: object) -> tuple[str, tuple[dict[str, Any], ...]]:
    plain: list[str] = []
    fallback: list[str] = []
    attachments: list[dict[str, Any]] = []

    def visit(part: object) -> None:
        if not isinstance(part, dict):
            return
        mime_type = str(part.get("mimeType") or "application/octet-stream")[:255]
        filename = part.get("filename")
        body = part.get("body")
        if isinstance(filename, str) and filename:
            attachments.append(
                {
                    "filename": filename[:255],
                    "mime_type": mime_type,
                    "size": (
                        int(body.get("size", 0))
                        if isinstance(body, dict) and isinstance(body.get("size"), int)
                        else 0
                    ),
                }
            )
        elif isinstance(body, dict):
            decoded = _decode_body(body.get("data"))
            if decoded:
                if mime_type.casefold() == "text/plain":
                    plain.append(decoded)
                elif mime_type.casefold() == "text/html":
                    fallback.append(decoded)
        children = part.get("parts")
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(payload)
    body_text = "\n".join(plain or fallback)
    return body_text[:50000], tuple(attachments[:20])


class GoogleGmailProvider:
    def __init__(self, token_file: str, *, timeout_seconds: float = 20.0) -> None:
        self.token_file = token_file
        self.timeout_seconds = timeout_seconds

    def _authorization_header(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        try:
            credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                self.token_file,
                scopes=[GMAIL_MODIFY_SCOPE],
            )
            if not credentials.valid:
                credentials.refresh(Request())
        except Exception as exc:
            raise GmailProviderError(
                "google_auth_invalid",
                "Google Gmail authorization is unavailable.",
                transient=False,
            ) from exc
        if not credentials.token:
            raise GmailProviderError(
                "google_auth_invalid",
                "Google did not provide an access token.",
                transient=False,
            )
        return f"Bearer {credentials.token}"

    @staticmethod
    def _url(suffix: str) -> str:
        return f"https://gmail.googleapis.com/gmail/v1/users/me/{suffix}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | Sequence[str]] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            response = httpx.request(
                method,
                url,
                headers={"Authorization": self._authorization_header()},
                params=params,
                json=json,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise GmailProviderError(
                "gmail_transient",
                "Gmail could not be reached.",
                transient=True,
            ) from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise GmailProviderError(
                "gmail_transient",
                f"Gmail returned HTTP {response.status_code}.",
                transient=True,
            )
        if response.status_code >= 400:
            code = (
                "google_auth_invalid"
                if response.status_code in {401, 403}
                else (
                    "gmail_message_not_found"
                    if response.status_code == 404
                    else "gmail_rejected"
                )
            )
            raise GmailProviderError(
                code,
                f"Gmail returned HTTP {response.status_code}.",
                transient=False,
            )
        return response

    @staticmethod
    def _mutation_result(
        metadata: GmailMessageMetadata,
        *,
        provider_request_id: str | None,
        disposition: str,
    ) -> GmailMutationResult:
        return GmailMutationResult(
            message_id=metadata.message_id,
            source_version=metadata.source_version,
            label_ids=metadata.label_ids,
            provider_request_id=provider_request_id,
            disposition=disposition,
        )

    def get_label_state(self, request: GmailMutationRequest) -> GmailMutationResult:
        metadata = self._message_metadata(request.message_id)
        if metadata is None:
            raise GmailProviderError(
                "gmail_message_not_found",
                "The Gmail message no longer exists.",
                transient=False,
            )
        return self._mutation_result(
            metadata,
            provider_request_id=None,
            disposition="observed",
        )

    def mutate_message(self, request: GmailMutationRequest) -> GmailMutationResult:
        current = self.get_label_state(request)
        if request.remove_label_id not in current.label_ids:
            return GmailMutationResult(
                message_id=current.message_id,
                source_version=current.source_version,
                label_ids=current.label_ids,
                provider_request_id=current.provider_request_id,
                disposition="already_applied",
            )
        url = self._url(f"messages/{quote(request.message_id, safe='')}/modify")
        try:
            response = httpx.request(
                "POST",
                url,
                headers={"Authorization": self._authorization_header()},
                json={
                    "addLabelIds": [],
                    "removeLabelIds": [request.remove_label_id],
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise GmailUnknownOutcome() from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise GmailUnknownOutcome()
        if response.status_code >= 400:
            code = (
                "google_auth_invalid"
                if response.status_code in {401, 403}
                else (
                    "gmail_message_not_found"
                    if response.status_code == 404
                    else "gmail_rejected"
                )
            )
            raise GmailProviderError(
                code,
                f"Gmail returned HTTP {response.status_code}.",
                transient=False,
            )
        document = response.json()
        if not isinstance(document, dict):
            raise GmailUnknownOutcome()
        metadata = _metadata(document)
        if (
            metadata.message_id != request.message_id
            or request.remove_label_id in metadata.label_ids
        ):
            raise GmailUnknownOutcome()
        return self._mutation_result(
            metadata,
            provider_request_id=response.headers.get("x-request-id"),
            disposition="modified",
        )

    def _profile_history_id(self) -> tuple[str, str | None]:
        response = self._request("GET", self._url("profile"))
        document = response.json()
        history_id = document.get("historyId") if isinstance(document, dict) else None
        if not isinstance(history_id, str) or not history_id:
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail returned a profile without a history cursor.",
                transient=False,
            )
        return history_id, response.headers.get("x-request-id")

    def _message_metadata(self, message_id: str) -> GmailMessageMetadata | None:
        try:
            response = self._request(
                "GET",
                self._url(f"messages/{quote(message_id, safe='')}"),
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject"],
                    "fields": _MESSAGE_FIELDS,
                },
            )
        except GmailProviderError as exc:
            if exc.code == "gmail_message_not_found":
                return None
            raise
        document = response.json()
        if not isinstance(document, dict):
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail returned an invalid message.",
                transient=False,
            )
        return _metadata(document)

    def _recovery_page(
        self,
        *,
        cursor: dict[str, Any],
        recovery_after: datetime,
    ) -> GmailScanPage:
        anchor = cursor.get("anchor_history_id")
        request_id: str | None = None
        if not isinstance(anchor, str):
            anchor, request_id = self._profile_history_id()
        raw_after = cursor.get("after_epoch")
        after_epoch = (
            int(raw_after)
            if isinstance(raw_after, int | str) and str(raw_after).isdigit()
            else int(recovery_after.timestamp())
        )
        parameters: dict[str, str] = {
            "q": f"after:{after_epoch}",
            "includeSpamTrash": "false",
            "maxResults": "100",
            "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate",
        }
        page_token = cursor.get("page_token")
        if isinstance(page_token, str) and page_token:
            parameters["pageToken"] = page_token
        response = self._request("GET", self._url("messages"), params=parameters)
        request_id = response.headers.get("x-request-id") or request_id
        document = response.json()
        if not isinstance(document, dict):
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail returned an invalid recovery page.",
                transient=False,
            )
        messages: list[GmailMessageMetadata] = []
        for reference in document.get("messages", []):
            if not isinstance(reference, dict) or not isinstance(reference.get("id"), str):
                continue
            metadata = self._message_metadata(str(reference["id"]))
            if metadata is not None:
                messages.append(metadata)
        next_page = document.get("nextPageToken")
        next_cursor = (
            {
                "mode": "recovery",
                "anchor_history_id": anchor,
                "after_epoch": after_epoch,
                "page_token": next_page,
            }
            if isinstance(next_page, str) and next_page
            else {"mode": "history", "history_id": anchor}
        )
        return GmailScanPage(
            messages=tuple(messages),
            next_cursor=next_cursor,
            observed_through=datetime.now(UTC),
            provider_request_id=request_id,
            recovery=True,
        )

    def _history_page(self, cursor: dict[str, Any]) -> GmailScanPage:
        start_history_id = cursor.get("start_history_id") or cursor.get("history_id")
        if not isinstance(start_history_id, str) or not start_history_id:
            raise GmailCursorInvalid()
        parameters: dict[str, str] = {
            "startHistoryId": start_history_id,
            "maxResults": "100",
            "fields": (
                "history(id,messagesAdded(message(id,threadId)),"
                "labelsAdded(message(id,threadId)),labelsRemoved(message(id,threadId))),"
                "historyId,nextPageToken"
            ),
        }
        page_token = cursor.get("page_token")
        if isinstance(page_token, str) and page_token:
            parameters["pageToken"] = page_token
        try:
            response = self._request("GET", self._url("history"), params=parameters)
        except GmailProviderError as exc:
            if exc.code in {"gmail_rejected", "gmail_message_not_found"}:
                raise GmailCursorInvalid() from exc
            raise
        document = response.json()
        if not isinstance(document, dict):
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail returned an invalid history page.",
                transient=False,
            )
        message_ids: set[str] = set()
        for history in document.get("history", []):
            if not isinstance(history, dict):
                continue
            for collection_name in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                collection = history.get(collection_name)
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    message = item.get("message") if isinstance(item, dict) else None
                    if isinstance(message, dict) and isinstance(message.get("id"), str):
                        message_ids.add(str(message["id"]))
        messages = tuple(
            metadata
            for message_id in sorted(message_ids)
            if (metadata := self._message_metadata(message_id)) is not None
        )
        current_history_id = document.get("historyId")
        if not isinstance(current_history_id, str) or not current_history_id:
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail history omitted its current cursor.",
                transient=False,
            )
        next_page = document.get("nextPageToken")
        next_cursor = (
            {
                "mode": "history",
                "start_history_id": start_history_id,
                "target_history_id": current_history_id,
                "page_token": next_page,
            }
            if isinstance(next_page, str) and next_page
            else {
                "mode": "history",
                "history_id": str(cursor.get("target_history_id") or current_history_id),
            }
        )
        return GmailScanPage(
            messages=messages,
            next_cursor=next_cursor,
            observed_through=datetime.now(UTC),
            provider_request_id=response.headers.get("x-request-id"),
            recovery=False,
        )

    def scan_page(
        self,
        *,
        cursor: dict[str, Any],
        recovery_after: datetime,
    ) -> GmailScanPage:
        if cursor.get("mode") == "history" and (
            cursor.get("history_id") or cursor.get("start_history_id")
        ):
            return self._history_page(cursor)
        return self._recovery_page(cursor=cursor, recovery_after=recovery_after)

    def read_message(self, message_id: str) -> GmailClaimedContent:
        response = self._request(
            "GET",
            self._url(f"messages/{quote(message_id, safe='')}"),
            params={"format": "full", "fields": _MESSAGE_FIELDS},
        )
        document = response.json()
        if not isinstance(document, dict):
            raise GmailProviderError(
                "gmail_invalid_response",
                "Gmail returned an invalid message.",
                transient=False,
            )
        metadata = _metadata(document)
        body_text, attachments = _extract_content(document.get("payload"))
        return GmailClaimedContent(
            message_id=metadata.message_id,
            thread_id=metadata.thread_id,
            source_version=metadata.source_version,
            sender=metadata.sender,
            subject=metadata.subject,
            label_ids=metadata.label_ids,
            body_text=body_text,
            attachments=attachments,
        )

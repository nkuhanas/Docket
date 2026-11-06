"""Google Tasks transport. Canonical reconciliation belongs to services.

Provider response bodies and credentials are deliberately absent from errors.
Mutations are never retried here, including inserts with an uncertain outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from docket.providers.google.oauth import (
    TASKS_READONLY_SCOPE,
    TASKS_SCOPE,
    authorized_user_file_status,
)

WriteMethod = Literal["insert", "patch", "move", "delete"]
_ROOT = "https://tasks.googleapis.com/tasks/v1"
_TASK_FIELDS = (
    "id,etag,title,notes,status,due,completed,updated,parent,position,deleted,hidden,"
    "assignmentInfo,webViewLink,links"
)
_MARKER = re.compile(r"\n\n\[Docket-Task-v1:([A-Za-z0-9_-]{8,128})\]$")


class TasksProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.transient = transient


class TasksUnknownOutcome(TasksProviderError):
    def __init__(self) -> None:
        super().__init__("google_tasks_unknown_outcome", "Google did not confirm the task change.")


@dataclass(frozen=True)
class TasksWriteGate:
    """Admitted method capabilities, populated only from scoped G0 evidence."""

    methods: frozenset[WriteMethod] = frozenset()
    credential_fingerprint: str | None = None

    def require(self, method: WriteMethod) -> None:
        if method not in self.methods:
            raise TasksProviderError(
                "google_tasks_method_unverified",
                f"Google Tasks {method} has not passed its provider validation.",
            )


def credential_fingerprint(path: Path) -> str | None:
    """Compare grant identity in memory; never return or log credential contents."""
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            return None
        client, refresh, scopes = (
            value.get(key) for key in ("client_id", "refresh_token", "scopes")
        )
        if (not isinstance(client, str) or not client or not isinstance(refresh, str) or not refresh
                or not isinstance(scopes, list)
                or not all(isinstance(item, str) for item in scopes)):
            return None
        return hashlib.sha256(json.dumps(
            [client, refresh, sorted(set(scopes))], separators=(",", ":"),
        ).encode()).hexdigest()
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class GoogleTask:
    task_id: str
    etag: str | None
    fields: dict[str, Any] = field(repr=False)

    @property
    def scheduled_date(self) -> date | None:
        value = self.fields.get("due")
        # Google due is a lexical calendar day, never an instant to timezone-shift.
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None


@dataclass(frozen=True)
class TasksPage:
    tasks: tuple[GoogleTask, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class TaskListsPage:
    lists: tuple[dict[str, Any], ...] = field(repr=False)
    next_page_token: str | None


class TasksProvider(Protocol):
    def list_task_lists(self, *, page_token: str | None = None) -> TaskListsPage: ...

    def list_tasks(
        self,
        task_list_id: str,
        *,
        page_token: str | None = None,
        updated_min: datetime | None = None,
        show_assigned: bool = False,
    ) -> TasksPage: ...

    def get_task(self, task_list_id: str, task_id: str) -> GoogleTask | None: ...

    def insert_task(
        self,
        task_list_id: str,
        fields: Mapping[str, Any],
        *,
        parent: str | None = None,
        previous: str | None = None,
    ) -> GoogleTask: ...

    def patch_task(
        self,
        task_list_id: str,
        task_id: str,
        fields: Mapping[str, Any],
        *,
        etag: str,
    ) -> GoogleTask: ...

    def move_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        etag: str,
        destination_task_list_id: str | None = None,
        parent: str | None = None,
        previous: str | None = None,
    ) -> GoogleTask: ...

    def delete_task(self, task_list_id: str, task_id: str, *, etag: str) -> None: ...


def transport_notes(notes: str, correlation: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", correlation):
        raise ValueError("Invalid task correlation")
    result = f"{notes}\n\n[Docket-Task-v1:{correlation}]"
    if len(result) > 8192:
        raise TasksProviderError(
            "google_tasks_notes_limit", "The task notes exceed Google's transport capacity."
        )
    return result


def canonical_notes(notes: str, expected_correlation: str) -> tuple[str, bool]:
    """Strip only an exact scoped suffix; unrecognized user text stays lossless."""
    match = _MARKER.search(notes)
    if match and match.group(1) == expected_correlation:
        return notes[: match.start()], True
    return notes, False


def creation_correlation(notes: str) -> str | None:
    """Extract transport data for scoped lookup; this is never authority or identity proof."""
    match = _MARKER.search(notes)
    return match.group(1) if match else None


def _document(response: httpx.Response) -> dict[str, Any]:
    try:
        result = response.json()
    except ValueError:
        result = None
    if not isinstance(result, dict):
        raise TasksProviderError(
            "google_tasks_invalid_response", "Google returned invalid task data."
        )
    return result


def _task(document: dict[str, Any]) -> GoogleTask:
    task_id = document.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise TasksProviderError(
            "google_tasks_invalid_response", "Google returned a task without ID."
        )
    etag = document.get("etag")
    return GoogleTask(task_id, etag if isinstance(etag, str) else None, document)


def _written_task(response: httpx.Response) -> GoogleTask:
    try:
        return _task(_document(response))
    except TasksProviderError as exc:
        # An unreadable successful response may still have applied the mutation.
        raise TasksUnknownOutcome() from exc


class GoogleTasksProvider:
    def __init__(
        self,
        token_file: str,
        *,
        gate: TasksWriteGate | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.token_file = token_file
        self.gate = gate or TasksWriteGate()
        self.timeout_seconds = timeout_seconds

    def _authorization_header(self, *, write: bool) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if write and (self.gate.credential_fingerprint is None
                      or self.gate.credential_fingerprint
                      != credential_fingerprint(Path(self.token_file))):
            raise TasksProviderError(
                "google_tasks_gate_changed", "Google Tasks method admission needs revalidation."
            )

        scope = TASKS_SCOPE
        if (
            not write
            and authorized_user_file_status(Path(self.token_file), required_scopes=[TASKS_SCOPE])
            != "configured"
        ):
            scope = TASKS_READONLY_SCOPE
        if (
            authorized_user_file_status(Path(self.token_file), required_scopes=[scope])
            != "configured"
        ):
            raise TasksProviderError(
                "google_tasks_scope_missing", "Google Tasks consent is required."
            )
        try:
            credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                self.token_file, scopes=[scope]
            )
            if not credentials.valid:
                credentials.refresh(Request())
            if not credentials.token:
                raise ValueError("Missing token")
        except Exception as exc:
            raise TasksProviderError(
                "google_tasks_auth_invalid", "Google Tasks authorization is unavailable."
            ) from exc
        return f"Bearer {credentials.token}"

    @staticmethod
    def _url(task_list_id: str, task_id: str | None = None) -> str:
        url = f"{_ROOT}/lists/{quote(task_list_id, safe='')}/tasks"
        return url if task_id is None else f"{url}/{quote(task_id, safe='')}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        fields: Mapping[str, Any] | None = None,
        etag: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": self._authorization_header(write=method != "GET")}
        if etag:
            headers["If-Match"] = etag
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                params=params,
                json=dict(fields) if fields is not None else None,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            if method != "GET":
                raise TasksUnknownOutcome() from exc
            raise TasksProviderError(
                "google_tasks_transient", "Google Tasks could not be reached.", transient=True
            ) from exc
        if response.status_code == 412:
            raise TasksProviderError(
                "google_tasks_stale", "Google changed; reconcile before retrying."
            )
        if method != "GET" and (response.status_code == 408 or response.status_code >= 500):
            raise TasksUnknownOutcome()
        if response.status_code == 429 or response.status_code >= 500:
            raise TasksProviderError(
                "google_tasks_transient", "Google Tasks is temporarily unavailable.", transient=True
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise TasksProviderError(
                "google_tasks_rejected", f"Google Tasks returned HTTP {response.status_code}."
            )
        return response

    @staticmethod
    def _require_found(response: httpx.Response) -> httpx.Response:
        if response.status_code == 404:
            raise TasksProviderError(
                "google_tasks_not_found", "The Google task or list is unavailable."
            )
        return response

    def list_task_lists(self, *, page_token: str | None = None) -> TaskListsPage:
        params = {"maxResults": "1000", "fields": "nextPageToken,items(id,title,etag,updated)"}
        if page_token:
            params["pageToken"] = page_token
        document = _document(
            self._require_found(self._request("GET", f"{_ROOT}/users/@me/lists", params=params))
        )
        return TaskListsPage(tuple(document.get("items", [])), document.get("nextPageToken"))

    def list_tasks(
        self,
        task_list_id: str,
        *,
        page_token: str | None = None,
        updated_min: datetime | None = None,
        show_assigned: bool = False,
    ) -> TasksPage:
        params = {
            "maxResults": "100",
            "showCompleted": "true",
            "showHidden": "true",
            "showDeleted": "true",
            "showAssigned": str(show_assigned).lower(),
            "fields": f"nextPageToken,items({_TASK_FIELDS})",
        }
        if page_token:
            params["pageToken"] = page_token
        if updated_min:
            if updated_min.tzinfo is None:
                raise ValueError("updated_min requires an instant")
            params["updatedMin"] = updated_min.astimezone(UTC).isoformat()
        document = _document(
            self._require_found(self._request("GET", self._url(task_list_id), params=params))
        )
        return TasksPage(
            tuple(_task(item) for item in document.get("items", [])), document.get("nextPageToken")
        )

    def get_task(self, task_list_id: str, task_id: str) -> GoogleTask | None:
        response = self._request(
            "GET", self._url(task_list_id, task_id), params={"fields": _TASK_FIELDS}
        )
        return None if response.status_code == 404 else _task(_document(response))

    def insert_task(
        self,
        task_list_id: str,
        fields: Mapping[str, Any],
        *,
        parent: str | None = None,
        previous: str | None = None,
    ) -> GoogleTask:
        self.gate.require("insert")
        params = {
            key: value
            for key, value in {"parent": parent, "previous": previous}.items()
            if value is not None
        }
        response = self._request("POST", self._url(task_list_id), params=params, fields=fields)
        return _written_task(self._require_found(response))

    @staticmethod
    def _etag(etag: str) -> None:
        if not etag.strip() or etag.strip() == "*":
            raise TasksProviderError(
                "google_tasks_etag_required", "A specific Google version is required."
            )

    def patch_task(
        self,
        task_list_id: str,
        task_id: str,
        fields: Mapping[str, Any],
        *,
        etag: str,
    ) -> GoogleTask:
        self.gate.require("patch")
        self._etag(etag)
        response = self._request(
            "PATCH", self._url(task_list_id, task_id), fields=fields, etag=etag
        )
        return _written_task(self._require_found(response))

    def move_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        etag: str,
        destination_task_list_id: str | None = None,
        parent: str | None = None,
        previous: str | None = None,
    ) -> GoogleTask:
        self.gate.require("move")
        self._etag(etag)
        params = {
            key: value
            for key, value in {
                "destinationTasklist": destination_task_list_id,
                "parent": parent,
                "previous": previous,
            }.items()
            if value is not None
        }
        response = self._request(
            "POST", f"{self._url(task_list_id, task_id)}/move", params=params, etag=etag
        )
        return _written_task(self._require_found(response))

    def delete_task(self, task_list_id: str, task_id: str, *, etag: str) -> None:
        self.gate.require("delete")
        self._etag(etag)
        self._require_found(self._request("DELETE", self._url(task_list_id, task_id), etag=etag))

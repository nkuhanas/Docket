"""Stateful Tasks transport for isolated reconciliation and concurrency tests."""

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, NoReturn

import httpx

from docket.providers.google.tasks import (
    GoogleTask,
    GoogleTasksProvider,
    TaskListsPage,
    TasksPage,
    TasksProviderError,
    TasksUnknownOutcome,
    TasksWriteGate,
)


class FakeTasksProvider(GoogleTasksProvider):
    def __init__(self, *, page_size: int = 100) -> None:
        super().__init__(
            "/unused", gate=TasksWriteGate(frozenset({"insert", "patch", "move", "delete"}))
        )
        self.lists: dict[str, dict[str, Any]] = {}
        self.tasks: dict[tuple[str, str], dict[str, Any]] = {}
        self.sequence = 0
        self.page_size = page_size
        self.uncertain_insert = False
        self.calls: list[tuple[str, str]] = []

    def seed_list(self, task_list_id: str, title: str) -> None:
        self.lists[task_list_id] = {"id": task_list_id, "title": title, "etag": '"list"'}

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        fields: Mapping[str, Any] | None = None,
        etag: str | None = None,
    ) -> httpx.Response:
        from docket.providers.google.tasks import _ROOT

        self.calls.append((method, url))
        params = params or {}
        if url == f"{_ROOT}/users/@me/lists":
            offset = int(params.get("pageToken", "0"))
            rows = list(self.lists.values())
            page: dict[str, Any] = {"items": deepcopy(rows[offset : offset + self.page_size])}
            if offset + self.page_size < len(rows):
                page["nextPageToken"] = str(offset + self.page_size)
            return httpx.Response(200, json=page)
        from urllib.parse import unquote

        parts = url.removeprefix(f"{_ROOT}/lists/").split("/")
        list_id = unquote(parts[0])
        if list_id not in self.lists:
            return httpx.Response(404)
        if len(parts) == 2 and method == "GET":
            rows = [row for (owner, _), row in self.tasks.items() if owner == list_id]
            if params.get("updatedMin"):
                instant = datetime.fromisoformat(params["updatedMin"])
                rows = [row for row in rows if datetime.fromisoformat(row["updated"]) >= instant]
            offset = int(params.get("pageToken", "0"))
            document: dict[str, Any] = {"items": deepcopy(rows[offset : offset + self.page_size])}
            if offset + self.page_size < len(rows):
                document["nextPageToken"] = str(offset + self.page_size)
            return httpx.Response(200, json=document)
        if len(parts) == 2 and method == "POST":
            self.sequence += 1
            task_id = f"fake-task-{self.sequence}"
            document = {"id": task_id, "status": "needsAction", **deepcopy(fields or {})}
            if "parent" in params:
                document["parent"] = params["parent"]
            key = (list_id, task_id)
        else:
            task_id = unquote(parts[2])
            key = (list_id, task_id)
            if key not in self.tasks:
                return httpx.Response(404)
            document = deepcopy(self.tasks[key])
            if method == "GET":
                return httpx.Response(200, json=document)
            if etag != document["etag"]:
                raise TasksProviderError(
                    "google_tasks_stale", "Google changed; reconcile before retrying."
                )
            self.sequence += 1
            subtree = {task_id}
            while True:
                found = {
                    identifier
                    for (owner, identifier), row in self.tasks.items()
                    if owner == list_id and row.get("parent") in subtree
                }
                if found.issubset(subtree):
                    break
                subtree.update(found)
            if method == "DELETE":
                document["deleted"] = True
                for descendant in subtree - {task_id}:
                    self.tasks[(list_id, descendant)]["deleted"] = True
            elif len(parts) > 3 and parts[3] == "move":
                target_list = params.get("destinationTasklist", list_id)
                if target_list not in self.lists:
                    return httpx.Response(404)
                if target_list != list_id:
                    del self.tasks[key]
                    key = (target_list, task_id)
                    for descendant in subtree - {task_id}:
                        child = self.tasks.pop((list_id, descendant))
                        child["etag"] = f'"fake-{self.sequence}-{descendant}"'
                        self.tasks[(target_list, descendant)] = child
                document["parent"] = params.get("parent")
                document["position"] = params.get("previous", "")
            else:
                document.update(deepcopy(fields or {}))
                if document.get("status") == "completed":
                    for descendant in subtree - {task_id}:
                        child = self.tasks[(list_id, descendant)]
                        if child.get("status") != "completed":
                            child.update(status="completed", completed=document["completed"])
                        child["etag"] = f'"fake-{self.sequence}-{descendant}"'
        document["etag"] = f'"fake-{self.sequence}"'
        document["updated"] = datetime.now(UTC).isoformat()
        self.tasks[key] = deepcopy(document)
        if method == "POST" and len(parts) == 2 and self.uncertain_insert:
            self.uncertain_insert = False
            raise TasksUnknownOutcome()
        return httpx.Response(204 if method == "DELETE" else 200, json=document)


class DisabledTasksProvider:
    @staticmethod
    def _disabled() -> NoReturn:
        raise TasksProviderError(
            "google_tasks_disabled", "Google Tasks synchronization is disabled."
        )

    def list_task_lists(self, *, page_token: str | None = None) -> TaskListsPage:
        self._disabled()
        raise AssertionError("unreachable")

    def list_tasks(
        self,
        task_list_id: str,
        *,
        page_token: str | None = None,
        updated_min: datetime | None = None,
        show_assigned: bool = False,
    ) -> TasksPage:
        self._disabled()
        raise AssertionError("unreachable")

    def get_task(self, task_list_id: str, task_id: str) -> GoogleTask | None:
        self._disabled()
        raise AssertionError("unreachable")

    def insert_task(self, *args: Any, **kwargs: Any) -> GoogleTask:
        self._disabled()
        raise AssertionError("unreachable")

    def patch_task(self, *args: Any, **kwargs: Any) -> GoogleTask:
        self._disabled()
        raise AssertionError("unreachable")

    def move_task(self, *args: Any, **kwargs: Any) -> GoogleTask:
        self._disabled()
        raise AssertionError("unreachable")

    def delete_task(self, *args: Any, **kwargs: Any) -> None:
        self._disabled()

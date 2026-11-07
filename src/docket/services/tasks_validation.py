"""Exercise Google Tasks methods only against explicitly supplied validation lists."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from docket.domain.errors import DocketError
from docket.providers.google.tasks import (
    GoogleTask,
    TasksProvider,
    TasksProviderError,
    canonical_notes,
    transport_notes,
)


def validate_tasks_methods(
    provider: TasksProvider,
    source: str,
    destination: str,
) -> dict[str, Any]:
    """Return actual checks, without retaining credentials or provider resource bodies."""
    if not source or source == destination:
        raise DocketError(
            code="tasks_validation_targets_invalid",
            message="Use two distinct dedicated validation lists.",
        )
    expected = {source: "Docket validation", destination: "Docket validation move target"}
    found: dict[str, str] = {}
    token = None
    visited: set[str] = set()
    while True:
        page = provider.list_task_lists(page_token=token)
        found.update(
            {row["id"]: row.get("title", "") for row in page.lists if row.get("id") in expected}
        )
        token = page.next_page_token
        if token is None:
            break
        if token in visited:
            raise DocketError(
                code="tasks_validation_paging_invalid", message="List discovery repeated."
            )
        visited.add(token)
    if found != expected:
        raise DocketError(
            code="tasks_validation_targets_invalid",
            message="Select the two dedicated Docket validation lists.",
        )
    created: set[str] = set()
    checks: dict[str, bool] = {}
    failures: dict[str, str] = {}
    uncertain_fixture = False

    def insert(**fields: Any) -> GoogleTask:
        task = provider.insert_task(source, {"title": "Docket API validation", **fields})
        created.add(task.task_id)
        return task

    def run(name: str, check: Callable[[], bool]) -> None:
        nonlocal uncertain_fixture
        try:
            checks[name] = check()
        except TasksProviderError as exc:
            checks[name], failures[name] = False, exc.code
            uncertain_fixture |= exc.code == "google_tasks_unknown_outcome"
        except Exception as exc:
            checks[name], failures[name] = False, type(exc).__name__

    def stale(call: Callable[[], Any]) -> bool:
        try:
            result = call()
            if isinstance(result, GoogleTask):
                created.add(result.task_id)
        except TasksProviderError as exc:
            return exc.code == "google_tasks_stale"
        return False

    def limits() -> bool:
        task = insert(title="T" * 1024, notes="N" * 8192)
        observed = provider.get_task(source, task.task_id)
        return bool(
            observed
            and observed.fields.get("title") == "T" * 1024
            and observed.fields.get("notes") == "N" * 8192
        )

    def dates() -> bool:
        day = (datetime.now(UTC) + timedelta(days=7)).date()
        task = insert(due=day.isoformat() + "T23:59:00Z")
        if task.scheduled_date != day:
            return False
        due = datetime.fromisoformat(task.fields["due"].replace("Z", "+00:00"))
        if (due.hour, due.minute, due.second, due.microsecond) != (0, 0, 0, 0):
            return False
        changed = provider.patch_task(
            source, task.task_id, {"title": "Date preserved"}, etag=task.etag or ""
        )
        cleared = provider.patch_task(source, task.task_id, {"due": None}, etag=changed.etag or "")
        return changed.scheduled_date == day and cleared.scheduled_date is None

    def patch() -> bool:
        task = insert()
        changed = provider.patch_task(
            source, task.task_id, {"title": "Fresh patch"}, etag=task.etag or ""
        )
        rejected = stale(
            lambda: provider.patch_task(
                source,
                task.task_id,
                {"notes": "Stale overwrite"},
                etag=task.etag or "",
            )
        )
        actual = provider.get_task(source, task.task_id)
        return bool(
            rejected
            and actual
            and actual.etag == changed.etag
            and actual.fields.get("notes") != "Stale overwrite"
        )

    def completion() -> bool:
        task = insert()
        completed = provider.patch_task(
            source, task.task_id, {"status": "completed"}, etag=task.etag or ""
        )
        if not completed.fields.get("completed"):
            return False
        reopened = provider.patch_task(
            source,
            task.task_id,
            {"status": "needsAction", "completed": None},
            etag=completed.etag or "",
        )
        return reopened.fields.get("status") == "needsAction" and not reopened.fields.get(
            "completed"
        )

    def move() -> bool:
        task = insert()
        current = provider.patch_task(
            source, task.task_id, {"title": "Fresh move"}, etag=task.etag or ""
        )
        if not stale(
            lambda: provider.move_task(
                source,
                task.task_id,
                etag=task.etag or "",
                destination_task_list_id=destination,
            )
        ):
            return False
        moved = provider.move_task(
            source, task.task_id, etag=current.etag or "", destination_task_list_id=destination
        )
        created.add(moved.task_id)
        old = provider.get_task(source, task.task_id)
        actual = provider.get_task(destination, moved.task_id)
        return bool(
            moved.task_id == task.task_id
            and actual
            and actual.task_id == task.task_id
            and old is None
        )

    def delete() -> bool:
        task = insert()
        current = provider.patch_task(
            source, task.task_id, {"title": "Fresh delete"}, etag=task.etag or ""
        )
        if not stale(lambda: provider.delete_task(source, task.task_id, etag=task.etag or "")):
            return False
        provider.delete_task(source, task.task_id, etag=current.etag or "")
        actual = provider.get_task(source, task.task_id)
        return bool(actual and actual.fields.get("deleted"))

    def correlation() -> bool:
        nonlocal uncertain_fixture
        marker = uuid.uuid4().hex
        # Deliberately discard the successful response. This exercises recovery
        # identity, not an assertion that a random network failure was injected.
        uncertain_fixture = True
        provider.insert_task(
            source,
            {
                "title": "Discarded response validation",
                "notes": transport_notes("Synthetic notes", marker),
            },
        )
        matches: list[GoogleTask] = []
        token = None
        seen: set[str] = set()
        while True:
            page = provider.list_tasks(source, page_token=token)
            matches.extend(
                task
                for task in page.tasks
                if not task.fields.get("deleted")
                and task.fields.get("title") == "Discarded response validation"
                and canonical_notes(task.fields.get("notes") or "", marker)
                == ("Synthetic notes", True)
            )
            token = page.next_page_token
            if token is None:
                break
            if token in seen:
                return False
            seen.add(token)
        if len(matches) != 1:
            return False
        created.add(matches[0].task_id)
        uncertain_fixture = False
        return True

    run("field_limits", limits)
    run("date_write", dates)
    run("patch_conditional_write", patch)
    run("completion_reopen", completion)
    run("move_conditional_identity", move)
    run("delete_conditional_tombstone", delete)
    run("discarded_insert_correlation", correlation)
    cleanup = not uncertain_fixture
    for identifier in created:
        for listing in (source, destination):
            try:
                actual = provider.get_task(listing, identifier)
                if actual is not None and not actual.fields.get("deleted"):
                    provider.delete_task(listing, identifier, etag=actual.etag or "")
            except TasksProviderError:
                cleanup = False
    return {
        "checks": checks,
        "safe_failures": failures,
        "created_records_cleaned": cleanup,
        "discarded_response_is_controlled_recovery_test": True,
        "capabilities": {
            "insert": {
                "uncertain_insert_recovery": checks.get("discarded_insert_correlation", False),
                "date_write": checks.get("date_write", False),
                "field_limits": checks.get("field_limits", False),
            },
            "patch": {
                "conditional_write": checks.get("patch_conditional_write", False),
                "date_write": checks.get("date_write", False),
                "completion_reopen": checks.get("completion_reopen", False),
            },
            "move": {
                "conditional_write": checks.get("move_conditional_identity", False),
                "stable_move_identity": checks.get("move_conditional_identity", False),
            },
            "delete": {"conditional_write": checks.get("delete_conditional_tombstone", False)},
        },
    }

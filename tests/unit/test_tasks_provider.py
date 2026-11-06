from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from docket.providers.google.tasks import (
    GoogleTasksProvider,
    TasksProviderError,
    TasksUnknownOutcome,
    TasksWriteGate,
    canonical_notes,
    transport_notes,
)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(GoogleTasksProvider, "_authorization_header", lambda self, **_: "synthetic")
    return GoogleTasksProvider(
        "/unused", gate=TasksWriteGate(frozenset({"insert", "patch", "move", "delete"}))
    )


def test_poll_requests_hidden_completed_deleted_and_resumes_pages(provider, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def request(method, url, **kwargs):
        calls.append(kwargs["params"])
        return httpx.Response(
            200,
            json={
                "items": [{"id": "one", "title": "x" * 1024, "due": "2026-11-13T00:00:00.000Z"}],
                "nextPageToken": "second-page",
            },
        )

    monkeypatch.setattr(httpx, "request", request)
    page = provider.list_tasks("list", updated_min=datetime(2026, 11, 6, tzinfo=UTC))
    provider.list_tasks("list", page_token=page.next_page_token)
    assert calls[0]["showCompleted"] == calls[0]["showHidden"] == calls[0]["showDeleted"] == "true"
    assert calls[0]["maxResults"] == "100"
    assert calls[0]["showAssigned"] == "false"
    assert calls[0]["updatedMin"] == "2026-11-06T00:00:00+00:00"
    assert calls[1]["pageToken"] == "second-page"
    assert page.tasks[0].scheduled_date == date(2026, 11, 13)
    assert len(page.tasks[0].fields["title"]) == 1024
    assert "xxxxxxxx" not in repr(page)


def test_unverified_methods_and_wildcard_versions_never_send_http(monkeypatch, provider) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("No provider write should occur")

    monkeypatch.setattr(httpx, "request", unexpected)
    with pytest.raises(TasksProviderError, match="has not passed"):
        GoogleTasksProvider("/unused").insert_task("list", {"title": "test"})
    with pytest.raises(TasksProviderError, match="specific Google version"):
        provider.delete_task("list", "task", etag="*")
    with pytest.raises(TasksProviderError, match="specific Google version"):
        provider.patch_task("list", "task", {"notes": None}, etag="")


def test_native_move_and_sparse_patch_pin_exact_etag(provider, monkeypatch) -> None:
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"id": "same", "etag": '"next"'})

    monkeypatch.setattr(httpx, "request", request)
    result = provider.move_task("old/list", "same", etag='"old"', destination_task_list_id="new")
    provider.patch_task("new", "same", {"notes": None}, etag='"next"')
    assert result.task_id == "same"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/lists/old%2Flist/tasks/same/move")
    assert calls[0][2]["params"] == {"destinationTasklist": "new"}
    assert calls[0][2]["headers"]["If-Match"] == '"old"'
    assert calls[1][2]["json"] == {"notes": None}


@pytest.mark.parametrize("failure", ["timeout", 500, 408])
def test_insert_unknown_outcome_has_no_retry_or_sensitive_error(
    provider, monkeypatch, failure
) -> None:
    calls = 0

    def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive upstream detail")
        return httpx.Response(failure, text="sensitive upstream detail")

    monkeypatch.setattr(httpx, "request", request)
    with pytest.raises(TasksUnknownOutcome) as caught:
        provider.insert_task("list", {"title": "private task"})
    assert not caught.value.transient
    assert calls == 1
    assert "sensitive" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_stale_delete_is_not_treated_as_success(provider, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(412))
    with pytest.raises(TasksProviderError) as caught:
        provider.delete_task("list", "task", etag='"old"')
    assert caught.value.code == "google_tasks_stale"


def test_disabled_api_has_actionable_error_without_private_diagnostics(provider, monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(403, json={
        "error": {"message": "private project and account details", "details": [
            {"reason": "SERVICE_DISABLED", "metadata": {"consumer": "private-project"}},
        ]},
    }))
    with pytest.raises(TasksProviderError) as caught:
        provider.list_task_lists()
    assert caught.value.code == "google_tasks_api_disabled"
    assert "Enable Google Tasks API" in str(caught.value)
    assert "private" not in str(caught.value)


def test_transport_marker_is_scoped_and_lossless() -> None:
    notes = "User notes\n\n[Docket-Task-v1:literal_user_text]"
    outgoing = transport_notes(notes, "binding_123")
    assert canonical_notes(outgoing, "binding_123") == (notes, True)
    assert canonical_notes(outgoing, "other_scope") == (outgoing, False)
    assert canonical_notes(notes, "binding_123") == (notes, False)
    assert canonical_notes(transport_notes("", "binding_123"), "binding_123") == ("", True)
    with pytest.raises(TasksProviderError, match="capacity"):
        transport_notes("x" * 8192, "binding_123")


def test_stateful_fake_keeps_unknown_insert_and_rejects_stale_delete() -> None:
    from docket.providers.google.fake_tasks import FakeTasksProvider

    fake = FakeTasksProvider(page_size=1)
    fake.seed_list("one", "Validation")
    fake.seed_list("two", "Moved")
    fake.uncertain_insert = True
    with pytest.raises(TasksUnknownOutcome):
        fake.insert_task("one", {"title": "Retained", "notes": transport_notes("", "binding_123")})
    recovered = fake.list_tasks("one").tasks[0]
    moved = fake.move_task(
        "one", recovered.task_id, etag=recovered.etag, destination_task_list_id="two"
    )
    with pytest.raises(TasksProviderError) as caught:
        fake.delete_task("two", moved.task_id, etag=recovered.etag)
    assert caught.value.code == "google_tasks_stale"
    assert fake.get_task("two", moved.task_id).fields["title"] == "Retained"


def test_disabled_provider_satisfies_transport_without_any_network(monkeypatch) -> None:
    from docket.providers.google.fake_tasks import DisabledTasksProvider

    def unexpected(*args, **kwargs):
        pytest.fail("Disabled transport cannot issue HTTP")

    monkeypatch.setattr(httpx, "request", unexpected)
    with pytest.raises(TasksProviderError) as caught:
        DisabledTasksProvider().list_task_lists()
    assert caught.value.code == "google_tasks_disabled"

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from docket.domain.errors import DocketError
from docket.models import GoogleTaskObservation, Task
from docket.services.task_reconciliation import (
    canonical_values,
    merge_fields,
    observed_values,
    task_patch,
)


def test_disjoint_local_title_and_google_notes_preserve_both_changes():
    base = {"title": "Original", "notes": "Before"}
    local = {"title": "Local title", "notes": "Before"}
    google = {"title": "Original", "notes": "Google notes"}
    merged = merge_fields(base, local, google, base)
    assert merged.inbound == {"notes": "Google notes"}
    assert merged.outbound == {"title": "Local title"}
    assert merged.conflicts == []
    assert merged.baseline == {"title": "Original", "notes": "Google notes"}


def test_divergent_title_does_not_block_an_independent_notes_import():
    base = {"title": "Original", "notes": "Before"}
    merged = merge_fields(
        base, {"title": "Local", "notes": "Before"}, {"title": "Google", "notes": "New notes"}, base
    )
    assert merged.conflicts == ["title"]
    assert merged.inbound == {"notes": "New notes"}


def test_unprojected_long_notes_are_never_replaced_by_an_unchanged_empty_copy():
    local = {"notes": "Preserve full canonical notes. " * 400}
    merged = merge_fields({}, local, {"notes": ""}, {"notes": ""}, limited={"notes"})
    assert merged.inbound == merged.outbound == {} and not merged.conflicts
    changed = merge_fields({}, local, {"notes": "Edited in Google"}, {"notes": ""})
    assert changed.conflicts == ["notes"] and not changed.inbound


def test_unchanged_google_open_state_does_not_reset_richer_local_state():
    base = {"status": "needsAction", "completed_at": None}
    task = Task(title="Blocked task", task_state="blocked", canonical_status="active")
    merged = merge_fields(base, canonical_values(task), dict(base), dict(base))
    assert task_patch(merged.inbound) == {}


def test_completed_import_uses_provider_time_and_rejects_missing_evidence():
    observation = GoogleTaskObservation(
        task_list_ref="synthetic-list",
        fields={
            "title": "Reported task",
            "status": "completed",
            "completed": "2026-11-06T12:30:00-08:00",
        },
    )
    values, _ = observed_values(Mock(), observation, None)
    patch = task_patch(values)
    assert patch["task_state"] == "completed"
    assert datetime.fromisoformat(patch["completed_at"]) == datetime(
        2026, 11, 6, 20, 30, tzinfo=UTC
    )
    observation.fields = {"title": "Reported task", "status": "completed"}
    with pytest.raises(DocketError) as caught:
        observed_values(Mock(), observation, None)
    assert caught.value.code == "google_task_invalid_time"

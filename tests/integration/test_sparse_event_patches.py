from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import func, select
from test_occurrence_mutations import _admit, _commit, _stage_cancel, _world

from docket.domain.errors import DocketError
from docket.models import (
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    EventOccurrence,
    Operation,
    ProviderEventBinding,
)
from docket.schemas.assembly import StageChangesInput
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.event_occurrences import identity_for_timing, occurrence_timing


@pytest.mark.parametrize(
    "location",
    ["Async", "Online", "TBD", "Room 121", "東京 🏫", "https://example.test/meeting", "", None],
)
def test_occurrence_location_only_preserves_baseline_and_commits_without_review(session, location):
    series, identity = _world(session)
    series.event_spec = {**series.event_spec, "location": "Old room", "notes": "Keep these notes"}
    original = deepcopy(series.event_spec)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, staged = _stage_cancel(
        session, series, scope, event_spec={"location": location}
    )
    assert staged.get("disposition") == "ready_to_commit", staged
    receipt = _commit(session, utterance, trace)
    assert receipt["ok"], receipt
    occurrence = session.scalar(select(EventOccurrence))
    child = session.scalar(
        select(CanonicalEvent).where(
            CanonicalEvent.ref_id == occurrence.replacement_event_ref,
        )
    )
    assert child.event_spec["location"] == location
    assert child.event_spec["notes"] == original["notes"]
    assert child.title == original["title"] == child.event_spec["title"]
    assert child.event_spec["calendar_lane"] == original["calendar_lane"]
    assert child.lane_ref == series.lane_ref
    assert child.event_spec["timing"]["start_local"] == "2026-09-08T15:00:00"
    assert child.event_spec["timing"]["end_local"] == "2026-09-08T15:50:00"
    assert series.event_spec == {
        **original,
        "recurrence": {**original["recurrence"], "excluded_dates": ["2026-09-07", "2026-09-08"]},
    }
    draft = session.scalar(select(ChangeSet))
    assert draft.staged_actions_json[0]["payload"] == {"event_spec": {"location": location}}
    assert session.scalar(select(func.count()).select_from(Operation)) == 2


def test_equal_occurrence_patch_is_noop_without_replacement_or_delivery(session):
    series, identity = _world(session)
    original = deepcopy(series.event_spec)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, staged = _stage_cancel(
        session, series, scope, event_spec={"title": series.title}
    )
    assert staged.get("disposition") == "ready_to_commit", staged
    assert staged["event_preview"][0]["no_op"]
    assert _commit(session, utterance, trace)["ok"]
    assert series.event_spec == original
    assert series.version == 1
    assert session.scalar(select(func.count()).select_from(EventOccurrence)) == 0
    assert session.scalar(select(func.count()).select_from(Operation)) == 0


def test_univ_oct8_location_only_exact_outcome_and_high_priority_preserved(session):
    series, _ = _world(session)
    series.title = "UNIV 1000"
    lane = "univ_1000"
    session.scalar(select(CalendarLane).where(CalendarLane.ref_id == series.lane_ref)).lane = lane
    series.event_spec = {
        **series.event_spec,
        "title": series.title,
        "calendar_lane": lane,
        "notes": "Read chapter 4",
        "priority": "high",
        "operator_tags": ["course"],
        "timing": {
            "kind": "timed",
            "start_local": "2026-09-17T14:00:00",
            "end_local": "2026-09-17T14:50:00",
            "timezone": "America/Los_Angeles",
            "fold": 0,
        },
        "recurrence": {"frequency": "weekly", "weekdays": ["TH"], "until_date": "2026-12-11"},
    }
    spec = StandaloneCalendarEventInput.model_validate(
        series.event_spec,
        context={"allow_explicit_priority": True},
    )
    series.event_spec = spec.model_dump(mode="json")
    identity = identity_for_timing(
        series.ref_id, occurrence_timing(spec, date(2026, 10, 8), fold=0)
    )
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, staged = _stage_cancel(
        session, series, scope, event_spec={"location": "Async"}
    )
    assert staged["disposition"] == "ready_to_commit", staged
    receipt = _commit(session, utterance, trace)
    assert receipt["disposition"] == "committed", receipt
    assert receipt["event_preview"][0]["changed_fields"] == ["location"]
    assert receipt["provider_disposition"] == "queued"
    child = session.scalar(select(CanonicalEvent).where(CanonicalEvent.ref_id != series.ref_id))
    assert child.event_spec == {
        **spec.model_dump(mode="json"),
        "location": "Async",
        "recurrence": None,
        "timing": occurrence_timing(spec, date(2026, 10, 8), fold=0).model_dump(mode="json"),
    }
    assert child.event_spec["calendar_lane"] == lane


@pytest.mark.parametrize("scope_kind", ["one_time", "entire_series"])
@pytest.mark.parametrize(
    "patch",
    [
        {"notes": None},
        {"notes": ""},
        {"notes": "New description"},
        {"title": "New title"},
        {"location": "Async"},
    ],
)
def test_sparse_master_edits_preserve_every_other_field(session, scope_kind, patch):
    series, _ = _world(session)
    series.event_spec = {
        **series.event_spec,
        "notes": "Existing notes",
        "location": "Existing room",
    }
    if scope_kind == "one_time":
        series.event_spec = {**series.event_spec, "recurrence": None}
    original = deepcopy(series.event_spec)
    utterance, trace, staged = _stage_cancel(
        session, series, {"kind": scope_kind}, event_spec=patch
    )
    assert staged["disposition"] == "ready_to_commit", staged
    assert _commit(session, utterance, trace)["disposition"] == "committed"
    assert series.event_spec == {**original, **patch}
    assert series.title == series.event_spec["title"]
    assert session.scalar(select(func.count()).select_from(EventOccurrence)) == 0
    draft = session.scalar(select(ChangeSet))
    assert draft.event_changes[0]["affected_fields"] == sorted(
        [f"event_spec.{key}" for key in patch] + (["title"] if "title" in patch else []),
    )


def test_one_time_equal_patch_has_no_canonical_or_provider_effect(session):
    series, _ = _world(session)
    series.event_spec = {**series.event_spec, "recurrence": None, "location": "Async"}
    utterance, trace, staged = _stage_cancel(
        session, series, {"kind": "one_time"}, event_spec={"location": "Async"}
    )
    assert staged["disposition"] == "ready_to_commit", staged
    receipt = _commit(session, utterance, trace)
    assert receipt["event_preview"][0]["no_op"]
    assert receipt["provider_operation_count"] == 0
    assert receipt["canonical_effect_count"] == 0
    assert series.version == 1


def _bind_child(session):
    occurrence = session.scalar(select(EventOccurrence))
    child = session.scalar(
        select(CanonicalEvent).where(
            CanonicalEvent.ref_id == occurrence.replacement_event_ref,
        )
    )
    master = session.scalar(select(ProviderEventBinding).limit(1))
    session.add(
        ProviderEventBinding(
            canonical_target_ref=child.ref_id,
            target_kind="event",
            account_id=master.account_id,
            calendar_id=master.calendar_id,
            provider_event_id="child",
            provider_etag='"child"',
            status="active",
        )
    )
    session.flush()
    return occurrence, child


def test_moved_then_content_edited_reuses_identity_and_series_override_stays_explicit(session):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    timing = {
        "kind": "timed",
        "start_local": "2026-09-09T16:00:00",
        "end_local": "2026-09-09T16:50:00",
        "timezone": "America/Los_Angeles",
    }
    utterance, trace, staged = _stage_cancel(session, series, scope, event_spec={"timing": timing})
    assert staged["disposition"] == "ready_to_commit"
    _commit(session, utterance, trace)
    occurrence, child = _bind_child(session)
    for number, patch in [(3, {"location": "Async"}), (4, {"notes": "Course reading"})]:
        utterance, trace, staged = _stage_cancel(
            session, series, scope, number=number, event_spec=patch
        )
        assert staged["disposition"] == "ready_to_commit", staged
        assert _commit(session, utterance, trace)["disposition"] == "committed"
        assert occurrence.replacement_event_ref == child.ref_id
        assert child.event_spec["timing"]["start_local"] == timing["start_local"]
    assert child.event_spec["location"] == "Async"
    assert child.event_spec["notes"] == "Course reading"
    before = deepcopy(child.event_spec)
    utterance, trace, staged = _stage_cancel(
        session,
        series,
        {"kind": "entire_series"},
        number=5,
        event_spec={"location": "New campus room"},
    )
    assert staged["disposition"] == "ready_to_commit", staged
    assert staged["event_preview"][0]["preserved_override_count"] == 1
    receipt = _commit(session, utterance, trace)
    assert receipt["event_preview"][0]["preserved_override_count"] == 1
    assert child.event_spec == before
    assert series.event_spec["recurrence"]["excluded_dates"] == ["2026-09-07", "2026-09-08"]


def test_cancelled_occurrence_field_patch_is_saved_with_actionable_error(session):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, _ = _stage_cancel(session, series, scope)
    _commit(session, utterance, trace)
    utterance, trace, staged = _stage_cancel(
        session, series, scope, number=3, event_spec={"location": "Async"}
    )
    assert staged["disposition"] == "saved_with_errors", staged
    assert staged["diagnostic_sample"][0]["code"] == "occurrence_cancelled"
    assert staged["diagnostic_sample"][0]["next_action"] == "resolve_occurrence_lifecycle"
    assert staged["diagnostic_sample"][0]["field_path"] == ["scope", "identity"]
    assert staged["diagnostic_sample"][0]["change_id"]
    assert session.scalar(select(EventOccurrence)).status == "cancelled"
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"location": "Another place"},
        {"location": "Async", "notes": "Unrelated"},
        {"status": "cancelled"},
    ],
)
def test_repair_cannot_reinterpret_recorded_field_intent(session, patch):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, _ = _stage_cancel(session, series, scope, event_spec={"location": "Async"})
    draft = session.scalar(select(ChangeSet))
    old_revision = draft.current_revision
    action = deepcopy(draft.staged_actions_json[0])
    action["payload"] = patch if "status" in patch else {"event_spec": patch}
    token = _admit(session, utterance, trace, "stage_changes", 2)
    with pytest.raises(DocketError) as caught:
        ChangeSetAssemblyService(session).stage(
            StageChangesInput(
                utterance_ref=utterance.ref_id,
                request_key=utterance.request_key,
                patch={"operations": [{"operation": "action_upsert", "action": action}]},
            ),
            assembly_operation_token=token,
            assembly_argument_hash="2" * 64,
        )
    assert caught.value.code == "event_patch_effect_conflict"
    assert draft.current_revision == old_revision
    assert draft.staged_actions_json[0]["payload"] == {"event_spec": {"location": "Async"}}


def test_canonical_change_between_stage_and_commit_cannot_be_merged_silently(session):
    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    utterance, trace, _ = _stage_cancel(session, series, scope, event_spec={"location": "Async"})
    series.version += 1
    series.event_spec = {**series.event_spec, "notes": "Concurrent update"}
    session.flush()
    with pytest.raises(DocketError) as caught:
        _commit(session, utterance, trace)
    assert caught.value.code == "changeset_validation_failed"
    assert any(error["code"] == "version_conflict" for error in caught.value.details["errors"])
    assert session.scalar(select(func.count()).select_from(Operation)) == 0
    assert series.event_spec["notes"] == "Concurrent update"


def test_old_compiler_draft_is_preserved_instead_of_silently_reinterpreted(session, monkeypatch):
    import docket.services.changeset_pins as pins

    series, identity = _world(session)
    scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
    # A previous compiler pin is not permission to silently rematerialize even
    # a syntactically current action. Unsupported full snapshots fail even earlier.
    with monkeypatch.context() as patcher:
        patcher.setattr(pins, "COMPILER_VERSION", 2)
        utterance, trace, _ = _stage_cancel(
            session, series, scope, event_spec={"location": "Async"},
        )
    draft = session.scalar(select(ChangeSet))
    revision = draft.current_revision
    retained = deepcopy(draft.staged_actions_json)
    token = _admit(session, utterance, trace, "stage_changes", 2)
    with pytest.raises(DocketError) as caught:
        ChangeSetAssemblyService(session).stage(
            StageChangesInput(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                patch={"operations": [{"operation": "action_upsert", "action": retained[0]}]},
            ), assembly_operation_token=token, assembly_argument_hash="2" * 64,
        )
    assert caught.value.code == "event_patch_migration_required"
    assert caught.value.details["authority_preserved"]
    assert draft.current_revision == revision
    assert draft.staged_actions_json == retained
    assert session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.parametrize("fold", [None, 0, 1])
def test_all_day_and_dst_fold_content_edits_preserve_original_identity(session, fold):
    series, _ = _world(session)
    timing = (
        {
            "kind": "all_day",
            "start_date": "2026-10-25",
            "end_date": "2026-10-26",
            "timezone": "America/Los_Angeles",
        }
        if fold is None
        else {
            "kind": "timed",
            "start_local": "2026-11-01T01:30:00",
            "end_local": "2026-11-01T01:50:00",
            "timezone": "America/Los_Angeles",
            "fold": fold,
        }
    )
    spec = StandaloneCalendarEventInput.model_validate(
        {
            **series.event_spec,
            "timing": timing,
            "recurrence": {"frequency": "weekly", "weekdays": ["SU"], "until_date": "2026-11-15"},
        }
    )
    series.event_spec = spec.model_dump(mode="json")
    selected = occurrence_timing(spec, date(2026, 11, 1), fold=fold)
    identity = identity_for_timing(series.ref_id, selected)
    utterance, trace, staged = _stage_cancel(
        session,
        series,
        {"kind": "occurrence", "identity": identity.model_dump(mode="json")},
        event_spec={"location": "Async"},
    )
    assert staged["disposition"] == "ready_to_commit", staged
    assert _commit(session, utterance, trace)["disposition"] == "committed"
    occurrence = session.scalar(select(EventOccurrence))
    child = session.scalar(
        select(CanonicalEvent).where(CanonicalEvent.ref_id == occurrence.replacement_event_ref)
    )
    assert occurrence.identity_json == identity.model_dump(mode="json")
    assert child.event_spec["timing"] == selected.model_dump(mode="json")

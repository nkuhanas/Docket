from datetime import date

import pytest
from sqlalchemy import select

from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import CanonicalEvent, ChangeSet, EventOccurrence, IntentSession
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.event_occurrences import OccurrenceReplacement
from docket.services.event_occurrences import (
    EventOccurrenceService,
    identity_for_timing,
    occurrence_timing,
)


def _world(session):
    basis = [new_public_ref("utt")]
    intent = IntentSession(source_utterance_ref=basis[0], conversation_ref="occurrence-test")
    session.add(intent)
    session.flush()
    changeset = ChangeSet(
        intent_session_id=intent.id,
        intent_session_ref=intent.ref_id,
        idempotency_key="occurrence-test",
        basis_refs=basis,
        state="validated",
    )
    session.add(changeset)
    session.flush()
    spec = StandaloneCalendarEventInput.model_validate(
        {
            "title": "MATH 1263",
            "calendar_lane": "math-1263",
            "timing": {
                "kind": "timed",
                "start_local": "2026-08-24T15:00:00",
                "end_local": "2026-08-24T15:50:00",
                "timezone": "America/Los_Angeles",
            },
            "recurrence": {
                "frequency": "weekly",
                "weekdays": ["MO", "TU", "TH", "FR"],
                "until_date": "2026-10-14",
                "excluded_dates": ["2026-09-07"],
            },
        }
    )
    series = CanonicalEvent(
        canonical_key="test-series",
        title=spec.title,
        status="active",
        authority="explicit_operator",
        event_spec=spec.model_dump(mode="json"),
        created_by_changeset_ref=changeset.ref_id,
        basis_refs=basis,
    )
    session.add(series)
    session.flush()
    identity = identity_for_timing(series.ref_id, occurrence_timing(spec, date(2026, 9, 8)))
    return series, changeset, basis, identity


def test_planning_preserves_neighbors_and_does_not_mutate(session) -> None:
    series, _changeset, _basis, identity = _world(session)
    original = dict(series.event_spec)
    plan = EventOccurrenceService(session).plan(identity)
    assert plan.master_after["recurrence"]["excluded_dates"] == ["2026-09-07", "2026-09-08"]
    assert series.event_spec == original
    assert series.status == "active" and series.version == 1
    assert session.scalar(select(EventOccurrence)) is None


def test_move_twice_then_cancel_twice_retains_original_identity(session) -> None:
    series, changeset, basis, identity = _world(session)
    service = EventOccurrenceService(session)
    replacement = OccurrenceReplacement.model_validate(
        {
            "title": "MATH 1263 — revised topic",
            "location": "Room 148",
            "timing": {
                "kind": "timed",
                "start_local": "2026-09-09T16:00:00",
                "end_local": "2026-09-09T16:50:00",
                "timezone": "America/Los_Angeles",
            },
        }
    )
    plan = service.plan(identity, replacement)
    child = CanonicalEvent(
        canonical_key="test-replacement",
        title=replacement.title,
        status="active",
        authority="explicit_operator",
        event_spec=plan.replacement_after,
        created_by_changeset_ref=changeset.ref_id,
        basis_refs=basis,
    )
    session.add(child)
    session.flush()
    series.event_spec = plan.master_after
    occurrence = service.record_applied(
        plan,
        changeset_ref=changeset.ref_id,
        basis_refs=basis,
        replacement_event_ref=child.ref_id,
    )
    original = dict(occurrence.identity_json)
    assert service.plan(identity, replacement).no_op
    second = replacement.model_copy(update={"title": "MATH 1263 — second revision"})
    plan2 = service.plan(identity, second)
    assert plan2.replacement_event_ref == child.ref_id
    child.event_spec = plan2.replacement_after
    service.record_applied(
        plan2, changeset_ref=changeset.ref_id, basis_refs=basis, replacement_event_ref=child.ref_id
    )
    cancelled = service.plan(identity)
    assert cancelled.replacement_event_ref == child.ref_id
    child.status = "cancelled"
    service.record_applied(
        cancelled,
        changeset_ref=changeset.ref_id,
        basis_refs=basis,
        replacement_event_ref=child.ref_id,
    )
    version = occurrence.version
    replay = service.plan(identity)
    assert replay.no_op
    service.record_applied(
        replay, changeset_ref=changeset.ref_id, basis_refs=basis, replacement_event_ref=child.ref_id
    )
    assert occurrence.version == version == 3
    assert occurrence.identity_json == original
    assert occurrence.original_local_date == date(2026, 9, 8)
    assert occurrence.original_timezone == "America/Los_Angeles"
    assert series.status == "active"
    assert series.event_spec["recurrence"]["excluded_dates"] == ["2026-09-07", "2026-09-08"]


def test_stale_occurrence_plan_cannot_overwrite_newer_disposition(session) -> None:
    _series, changeset, basis, identity = _world(session)
    service = EventOccurrenceService(session)
    plan = service.plan(identity)
    service.record_applied(
        plan, changeset_ref=changeset.ref_id, basis_refs=basis, replacement_event_ref=None
    )
    with pytest.raises(DocketError) as exc:
        service.record_applied(
            plan, changeset_ref=changeset.ref_id, basis_refs=basis, replacement_event_ref=None
        )
    assert exc.value.code == "occurrence_version_conflict"

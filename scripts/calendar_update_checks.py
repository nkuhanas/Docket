"""Exact provider-precondition locking in the isolated PostgreSQL smoke database."""

from calendar_reauthorization_checks import _committed_delivery
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from docket.domain.errors import DocketError
from docket.models import CalendarLane, CanonicalEvent, ProviderEventBinding
from docket.services.calendar_update_plan import (
    compile_event_update_plan,
    validate_event_update_plan,
)


def test_calendar_update_binding_pin_serializes_with_provider_observations(factory) -> None:
    _, _, event_id, _ = _committed_delivery(factory, "1542799000000000983")
    with factory.begin() as session:
        event = session.get(CanonicalEvent, event_id)
        lane = session.scalar(select(CalendarLane).where(CalendarLane.ref_id == event.lane_ref))
        binding = ProviderEventBinding(
            canonical_target_ref=event.ref_id, target_kind="event", account_id=lane.account_id,
            calendar_id=lane.calendar_id, provider_event_id="scoped-update-smoke",
            provider_etag='"observed"', status="diverged", version=1,
        )
        session.add(binding)
        session.flush()
        binding_id, event_ref, lane_id = binding.id, event.ref_id, lane.id
        plan = compile_event_update_plan(
            session, event, {"event_spec": {**event.event_spec, "notes": "New description"}}, lane,
        )
        assert plan["event_patch_fields"] == ["description"]

    with factory() as committing:
        lane = committing.get(CalendarLane, lane_id)
        # The validation lock must remain held through canonical application and
        # intent insertion, until the *caller's* transaction commits.
        validate_event_update_plan(
            committing, target_ref=event_ref, lane=lane, parameters=plan, lock=True,
        )
        try:
            with factory.begin() as observing:
                observing.execute(text("SET LOCAL lock_timeout = '100ms'"))
                observing.execute(update(ProviderEventBinding).where(
                    ProviderEventBinding.id == binding_id,
                ).values(provider_etag='"concurrent"', version=2))
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "55P03"
        else:
            raise AssertionError("A provider observation bypassed the committing binding lock")
        committing.commit()

        # A long-lived session may have the previous ORM object cached. The
        # next validation must read the locked DB row, not that cached object.
        assert committing.get(ProviderEventBinding, binding_id).provider_etag == '"observed"'
        with factory.begin() as observing:
            observing.execute(update(ProviderEventBinding).where(
                ProviderEventBinding.id == binding_id,
            ).values(provider_etag='"concurrent"', version=2))
        try:
            validate_event_update_plan(
                committing, target_ref=event_ref, lane=lane, parameters=plan, lock=True,
            )
        except DocketError as exc:
            assert exc.code == "provider_event_version_conflict"
        else:
            raise AssertionError("An old observed revision accepted a newer provider version")
        committing.rollback()

"""An observed Google edit is not a missing event or permission to overwrite it."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_calendar_read_model import _timed
from test_canonical_event_changesets import _capture, _commit_event_change, _commit_rich_event
from test_changeset_assembly import _admit

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CanonicalEvent,
    ChangeSet,
    Operation,
    OperationTarget,
    OperatorUtterance,
    ProviderEventBinding,
    SemanticRequest,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.assembly import StageChangesInput
from docket.services.calendar_sync import CalendarSyncService
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.operations import OperationRunner

NOTES = "Kennedy Library fishbowl, space 216S. Reservation 4:30-6 PM."


def _fixture(factory, *, diverged=True):
    with factory.begin() as session:
        event_ref, _, _ = _commit_rich_event(session, message_id="1542899000000000301")
    provider = FakeCalendarProvider()
    runner = OperationRunner(factory, provider)
    assert runner.run_due_once() and runner.run_due_once()
    if diverged:
        _google_edit(factory, provider, title="Google-side title")
    return event_ref, provider, runner


def _google_edit(factory, provider, *, title):
    with factory() as session:
        binding = session.scalar(select(ProviderEventBinding))
        account_id, calendar_id, event_id = (
            binding.account_id, binding.calendar_id, binding.provider_event_id,
        )
    previous = provider.events[event_id]
    provider.events[event_id] = replace(
        previous, provider_etag=f'"{title}"',
        snapshot={**previous.snapshot, "summary": title},
    )
    observed = replace(
        _timed(event_id, datetime(2026, 9, 5, 1, tzinfo=UTC), summary=title),
        location="Cal Poly", provider_etag=f'"{title}"',
    )
    provider.put_snapshot_event(observed)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    sync = CalendarSyncService(
        factory, provider, settings, clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert sync.sync_target(account_id, calendar_id, force=True)
    with factory() as session:
        binding = session.scalar(select(ProviderEventBinding))
        assert binding.status == "diverged"
        assert binding.provider_etag == f'"{title}"'


def _mutation(event):
    return {
        "mutation_type": "canonical_event_modify", "action": "update",
        # The ordinary contract carries the full canonical spec, but copied
        # title/time/location fields must not become unwanted provider writes.
        "payload": {"event_spec": {**event.event_spec, "notes": NOTES}},
        "affected_fields": ["event_spec"],
    }


@pytest.mark.parametrize("lost_response", [False, True])
def test_description_update_preserves_google_edit_and_reconciles_exact_patch(
    session_factory, lost_response,
):
    event_ref, provider, runner = _fixture(session_factory)
    with session_factory.begin() as session:
        event = session.scalar(select(CanonicalEvent))
        original_timing = event.event_spec["timing"]
        result = _commit_event_change(
            session, event_ref=event_ref, message_id="1542899000000000302",
            text="Update only the existing meeting description from this notice.",
            mutation=_mutation(event),
        )
        assert result["state"] == "committed", result
        target = session.scalar(select(OperationTarget).join(Operation).where(
            Operation.operation_type == "calendar_update_event",
        ))
        assert target.parameters["event_patch_fields"] == ["description"]
        assert target.parameters["provider_etag"] == '"Google-side title"'
        assert event.title == "PolyUAS general meeting"
        assert event.event_spec["timing"] == original_timing
        assert event.event_spec["notes"] == NOTES
    if lost_response:
        provider.next_update_outcome = "unknown_after_write"
    assert runner.run_due_once()
    if lost_response:
        with session_factory.begin() as session:
            operation = session.scalar(select(Operation).where(
                Operation.operation_type == "calendar_update_event",
            ))
            assert operation.status == "reconciliation_required"
            operation.next_attempt_at = datetime.now(UTC)
        # New worker, same durable operation: reconcile, never replay the write.
        assert OperationRunner(session_factory, provider).reconcile_once()
    assert len(provider.events) == 1
    delivered = next(iter(provider.events.values()))
    assert delivered.snapshot["summary"] == "Google-side title"
    assert delivered.snapshot["location"] == "Cal Poly"
    assert delivered.snapshot["start"]["dateTime"] == "2026-09-04T18:00:00"
    assert delivered.snapshot["end"]["dateTime"] == "2026-09-04T19:00:00"
    assert delivered.snapshot["description_sha256"] == sha256_json(NOTES)
    assert "description" not in delivered.snapshot
    with session_factory() as session:
        binding = session.scalar(select(ProviderEventBinding))
        assert binding.status == "diverged"  # unrelated discrepancy was not erased/adopted
        assert session.scalar(select(Operation.status).where(
            Operation.operation_type == "calendar_update_event",
        )) == "succeeded"


def test_google_edit_after_commit_is_rejected_without_overwriting(session_factory):
    event_ref, provider, runner = _fixture(session_factory)
    with session_factory.begin() as session:
        event = session.scalar(select(CanonicalEvent))
        result = _commit_event_change(
            session, event_ref=event_ref, message_id="1542899000000000303",
            text="Update only the description.", mutation=_mutation(event),
        )
        assert result["state"] == "committed"
    _google_edit(session_factory, provider, title="Another Google edit")
    assert runner.run_due_once()
    delivered = next(iter(provider.events.values()))
    assert delivered.snapshot["summary"] == "Another Google edit"
    assert delivered.snapshot["description_sha256"] != sha256_json(NOTES)
    with session_factory() as session:
        operation = session.scalar(select(Operation).where(
            Operation.operation_type == "calendar_update_event",
        ))
        assert operation.status == "failed"
        assert operation.last_error_code == "google_calendar_precondition_failed"


def test_stage_commit_race_preserves_authority_and_same_draft_for_restage(session_factory):
    event_ref, provider, runner = _fixture(session_factory)
    trace = new_public_ref("trace")
    with session_factory.begin() as session:
        utterance_ref, _ = _capture(
            session, message_id="1542899000000000304", text="Update only the meeting description.",
        )
        utterance = session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id == utterance_ref,
        ))
        event = session.scalar(select(CanonicalEvent))
        request = StageChangesInput.model_validate({
            "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
            "expected_versions": {event_ref: event.version},
            "assembly_scope": {
                "resolved_intent": {"intent": "update meeting description"},
                "target_refs": [event_ref], "allowed_mutation_types": ["canonical_event_modify"],
            },
            "patch": {"operations": [{"operation": "action_upsert", "action": {
                **_mutation(event), "change_id": "update-description",
                "object_type": "canonical_event", "object_ref": event_ref,
                "basis_refs": [utterance.ref_id],
            }}]},
        })
        token = _admit(
            session, utterance=utterance, trace_ref=trace, call_id="stage", ordinal=1,
            tool_name="docket_stage_changes", argument_hash="a" * 64,
        )
        receipt = ChangeSetAssemblyService(session).stage(
            request, assembly_operation_token=token, assembly_argument_hash="a" * 64,
        )
        assert receipt["disposition"] == "ready_to_commit", str(receipt)
        draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == receipt["draft_ref"]))
        draft_ref, authority_hash, request_ref = (
            draft.ref_id, draft.authority_scope_hash, draft.semantic_request_ref,
        )
    _google_edit(session_factory, provider, title="Changed after staging")
    with session_factory.begin() as session:
        utterance = session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id == utterance_ref,
        ))
        service = ChangeSetAssemblyService(session)
        token = _admit(
            session, utterance=utterance, trace_ref=trace, call_id="commit", ordinal=2,
            tool_name="docket_commit_changeset", argument_hash="b" * 64,
        )
        with pytest.raises(DocketError) as blocked:
            service.commit(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                assembly_operation_token=token, assembly_argument_hash="b" * 64,
            )
        assert blocked.value.code == "changeset_validation_failed"
        # The authenticated MCP boundary terminalizes this service rejection;
        # exercise that same durable path before admitting the repair operation.
        assert service.reject_admitted_operation(
            token=token, argument_hash="b" * 64, operation_kind="commit",
            utterance_ref=utterance.ref_id, error=blocked.value,
        ) is not None
        draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == draft_ref))
        assert any(e["code"] == "provider_event_version_conflict" for e in draft.validation_errors)
        assert draft.state != "committed"
        assert session.scalar(select(CanonicalEvent)).event_spec.get("notes") is None
        assert session.scalar(select(func.count(Operation.id)).where(
            Operation.operation_type == "calendar_update_event",
        )) == 0
        token = _admit(
            session, utterance=utterance, trace_ref=trace, call_id="restage", ordinal=3,
            tool_name="docket_stage_changes", argument_hash="c" * 64,
        )
        refreshed = service.stage(
            request, assembly_operation_token=token, assembly_argument_hash="c" * 64,
        )
        assert refreshed["disposition"] == "ready_to_commit", refreshed
        assert draft.authority_scope_hash == authority_hash
        assert draft.semantic_request_ref == request_ref
        token = _admit(
            session, utterance=utterance, trace_ref=trace, call_id="commit-new", ordinal=4,
            tool_name="docket_commit_changeset", argument_hash="d" * 64,
        )
        committed = service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            assembly_operation_token=token, assembly_argument_hash="d" * 64,
        )
        assert committed["disposition"] == "committed", committed
        assert session.scalar(select(SemanticRequest).where(
            SemanticRequest.ref_id == request_ref,
        )).authority_availability == "consumed_committed"
    assert runner.run_due_once()
    assert next(iter(provider.events.values())).snapshot["summary"] == "Changed after staging"


def test_cancelled_binding_is_not_misreported_as_missing(session_factory):
    event_ref, _, _ = _fixture(session_factory, diverged=False)
    with session_factory.begin() as session:
        session.scalar(select(ProviderEventBinding)).status = "cancelled"
        event = session.scalar(select(CanonicalEvent))
        result = _commit_event_change(
            session, event_ref=event_ref, message_id="1542899000000000305",
            text="Update the description.", mutation=_mutation(event),
        )
        assert result["disposition"] == "rejected_validation"
        draft = session.scalar(select(ChangeSet).where(ChangeSet.state == "draft"))
        error = next(e for e in draft.validation_errors
                     if e["code"] == "provider_event_binding_not_ready")
        assert error["details"]["binding_state"] == "cancelled"
        assert event.event_spec.get("notes") is None


def test_diverged_binding_does_not_permit_replacing_timing_or_recurrence(session_factory):
    event_ref, _, _ = _fixture(session_factory)
    with session_factory.begin() as session:
        event = session.scalar(select(CanonicalEvent))
        mutation = _mutation(event)
        mutation["payload"]["event_spec"]["timing"] = {
            **event.event_spec["timing"], "start_local": "2026-09-04T18:30:00",
        }
        result = _commit_event_change(
            session, event_ref=event_ref, message_id="1542899000000000306",
            text="Change the meeting start time.", mutation=mutation,
        )
        assert result["disposition"] == "rejected_validation"
        draft = session.scalar(select(ChangeSet).where(ChangeSet.state == "draft"))
        error = next(e for e in draft.validation_errors
                     if e["code"] == "provider_event_binding_not_ready")
        assert error["details"]["next_action"] == "reconcile_provider_timing_or_recurrence"
        assert event.event_spec["timing"]["start_local"] == "2026-09-04T18:00:00"

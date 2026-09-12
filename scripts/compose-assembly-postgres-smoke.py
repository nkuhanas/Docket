"""Exercise ChangeSet assembly's PostgreSQL-only locking and race invariants."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.database import configure_database
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    AttachmentEvidence,
    CalendarDateBinding,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    DeferredIngress,
    EventOccurrence,
    ExecutionAttempt,
    ExecutionLease,
    LaneRoutingDecision,
    Operation,
    OperationTarget,
    OperatorUtterance,
    ProviderAccount,
    ProviderEventBinding,
    Source,
    ToolInvocation,
)
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.event_occurrences import (
    bind_calendar_date,
    identity_for_timing,
    occurrence_timing,
)
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.history import HistoryService
from docket.services.invocation_binding import bind_invocation
from docket.services.operations import OperationRunner
from docket.services.provenance import ProvenanceService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def test_occurrence_commits_serialize_and_identity_is_immutable(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        first = _utterance("1542799000000000651", "Cancel Tuesday's class, not the series.")
        second = _utterance("1542799000000000652", "Cancel that same class occurrence.")
        session.add_all([first, second])
        session.flush()
        account = ProviderAccount(
            provider="google",
            external_account_id="occurrence-smoke",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="occurrence-smoke",
            display_name="Occurrence smoke",
            calendar_id="occurrence@example.com",
            color_hex="#3367D6",
            status="active",
            basis_refs=[first.ref_id],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane)
        session.flush()
        spec = StandaloneCalendarEventInput.model_validate(
            {
                "title": "Occurrence smoke",
                "calendar_lane": lane.lane,
                "timing": {
                    "kind": "timed",
                    "start_local": "2026-09-07T15:00:00",
                    "end_local": "2026-09-07T15:50:00",
                    "timezone": "America/Los_Angeles",
                },
                "recurrence": {
                    "frequency": "weekly",
                    "weekdays": ["MO", "TU"],
                    "until_date": "2026-10-14",
                    "excluded_dates": ["2026-09-07"],
                },
            }
        )
        series = CanonicalEvent(
            canonical_key="occurrence-smoke",
            title=spec.title,
            event_spec=spec.model_dump(mode="json"),
            status="active",
            authority="explicit_operator",
            lane_ref=lane.ref_id,
            lane_id=lane.id,
            basis_refs=[first.ref_id],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(series)
        session.flush()
        route = LaneRoutingDecision(
            event_ref=series.ref_id,
            lane_ref=lane.ref_id,
            lane_id=lane.id,
            decision_kind="explicit_operator",
            operator_confirmed=True,
            basis_refs=[first.ref_id],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add_all(
            [
                route,
                ProviderEventBinding(
                    canonical_target_ref=series.ref_id,
                    target_kind="event",
                    account_id=account.id,
                    calendar_id=lane.calendar_id,
                    provider_event_id="occurrence-master",
                    status="active",
                ),
            ]
        )
        session.flush()
        series.routing_decision_ref = route.ref_id
        series_ref = series.ref_id
        identity = identity_for_timing(series_ref, occurrence_timing(spec, date(2026, 9, 8)))
        requests = [(first.ref_id, first.request_key), (second.ref_id, second.request_key)]
    pending = []
    for utterance_ref, request_key in requests:
        trace_ref = new_public_ref("trace")
        token = _admit_committed(
            factory,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id="occurrence-stage",
            ordinal=1,
            tool_name="docket_stage_changes",
            argument_hash="a" * 64,
        )
        scope = {"kind": "occurrence", "identity": identity.model_dump(mode="json")}
        request = StageChangesInput.model_validate(
            {
                "utterance_ref": utterance_ref,
                "request_key": request_key,
                "expected_versions": {series_ref: 1},
                "assembly_scope": {
                    "resolved_intent": {"intent": "cancel selected occurrence"},
                    "allowed_mutation_types": ["canonical_event_cancel"],
                    "target_refs": [series_ref],
                    "event_scopes": {series_ref: scope},
                },
                "patch": {
                    "operations": [
                        {
                            "operation": "action_upsert",
                            "action": {
                                "mutation_type": "canonical_event_cancel",
                                "action": "retract",
                                "object_type": "canonical_event",
                                "change_id": "cancel-class",
                                "object_ref": series_ref,
                                "scope": scope,
                                "affected_fields": ["status"],
                                "basis_refs": [utterance_ref],
                            },
                        }
                    ]
                },
            }
        )
        staged = _stage(
            factory,
            utterance_ref=utterance_ref,
            token=token,
            argument_hash="a" * 64,
            request=request,
        )
        assert staged["assembly_ready"], staged
        commit_token = _admit_committed(
            factory,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id="occurrence-commit",
            ordinal=2,
            tool_name="docket_commit_changeset",
            argument_hash="b" * 64,
        )
        pending.append((utterance_ref, request_key, commit_token))
    barrier = threading.Barrier(2)

    def commit(binding: tuple[str, str, str]) -> dict[str, Any]:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            service = ChangeSetAssemblyService(session)
            try:
                with session.begin_nested():
                    return service.commit(
                        utterance_ref=binding[0],
                        request_key=binding[1],
                        assembly_operation_token=binding[2],
                        assembly_argument_hash="b" * 64,
                    )
            except DocketError as exc:
                assert exc.code == "changeset_validation_failed", exc.code
                assert any(error["code"] == "version_conflict" for error in exc.details["errors"])
                result = service.reject_admitted_operation(
                    token=binding[2],
                    argument_hash="b" * 64,
                    operation_kind="commit",
                    utterance_ref=binding[0],
                    error=exc,
                )
                assert result is not None
                return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(commit, pending))
    assert sum(result["disposition"] == "committed" for result in outcomes) == 1, outcomes
    with factory.begin() as session:
        series = session.scalar(select(CanonicalEvent).where(CanonicalEvent.ref_id == series_ref))
        assert series.status == "active"
        assert series.event_spec["recurrence"]["excluded_dates"] == ["2026-09-07", "2026-09-08"]
        assert (
            session.scalar(
                select(func.count(EventOccurrence.id)).where(
                    EventOccurrence.series_ref == series_ref
                )
            )
            == 1
        )
    # Exercise database triggers, bypassing the complementary ORM guards.
    for statement in (
        "UPDATE event_occurrences SET original_timezone='UTC' WHERE series_ref=:ref",
        "UPDATE event_occurrences SET original_start_key='changed' WHERE series_ref=:ref",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(statement), {"ref": series_ref})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL permitted an occurrence identity rewrite")


def test_relative_date_capture_serializes(factory: sessionmaker[Session]) -> None:
    utterance_ref, _ = _create_utterance(factory, "1542799000000000653", "Cancel tomorrow.")
    barrier = threading.Barrier(2)

    def bind(zone: str) -> dict[str, Any]:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            return bind_calendar_date(
                session, utterance_ref=utterance_ref, relative_day="tomorrow", timezone=zone
            ).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(bind, ["America/Los_Angeles", "Asia/Tokyo"]))
    assert values[0] == values[1]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(CalendarDateBinding.id)).where(
                    CalendarDateBinding.utterance_ref == utterance_ref
                )
            )
            == 1
        )
    try:
        with factory.begin() as session:
            session.execute(
                text("UPDATE calendar_date_bindings SET timezone='UTC' WHERE utterance_ref=:ref"),
                {"ref": utterance_ref},
            )
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL permitted a relative-date binding rewrite")


def test_native_and_deferred_ingress_claim_once(factory: sessionmaker[Session]) -> None:
    settings = get_settings()
    with factory.begin() as session:
        gateway = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(), instance_kind="hermes_discord_gateway"
        )
        utterance = _utterance("1542799000000000550", "One input, one execution.")
        session.add(utterance)
        session.flush()
        session.add(
            DeferredIngress(
                source_key=utterance.request_key,
                ingress_kind="typed_message",
                utterance_ref=utterance.ref_id,
                status="pending",
            )
        )
        utterance_ref = utterance.ref_id
        request = OperatorUtteranceCapture(
            request_id=uuid.uuid4(),
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id="1542799000000000550",
            actor_id=settings.operator_discord_user_id,
            verbatim_text=utterance.verbatim_text,
            request_key=utterance.request_key,
            gateway_instance_ref=str(gateway["ref"]),
        )
    barrier = threading.Barrier(2)

    def capture() -> dict[str, Any]:
        with factory.begin() as session:
            # Both transactions may have observed the pending state before claim.
            session.scalar(
                select(DeferredIngress).where(DeferredIngress.utterance_ref == utterance_ref)
            )
            barrier.wait(timeout=10)
            return ProvenanceService(session).capture_operator_utterance(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(capture), executor.submit(capture)
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sum(bool(row["deferred_ingress"]["execution_completion_token"]) for row in results) == 1
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(ExecutionLease.id)).where(
                    ExecutionLease.subject_ref == utterance_ref
                )
            )
            == 1
        )


def _utterance(message_id: str, text: str) -> OperatorUtterance:
    settings = get_settings()
    return OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=(
            f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}"
        ),
        said_at=datetime.now(UTC),
        verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
    )


def _load_utterance(session: Session, utterance_ref: str) -> OperatorUtterance:
    utterance = session.scalar(
        select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
    )
    assert utterance is not None
    return utterance


def _admit(
    session: Session,
    *,
    utterance_ref: str,
    trace_ref: str,
    call_id: str,
    ordinal: int,
    tool_name: str,
    argument_hash: str,
) -> str:
    settings = get_settings()
    utterance = _load_utterance(session, utterance_ref)
    admitted = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=trace_ref,
        upstream_tool_call_id=call_id,
        trace_ordinal=ordinal,
        tool_name=tool_name,
        argument_hash=argument_hash,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        source_message_id=utterance.request_key.split(":")[3],
        actor_id=settings.operator_discord_user_id,
    )
    return str(admitted["assembly_operation_token"])


def _item_stage(
    utterance: OperatorUtterance,
    *,
    change_id: str,
    title: str,
    include_scope: bool,
) -> StageChangesInput:
    return StageChangesInput.model_validate(
        {
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "assembly_scope": (
                {
                    "resolved_intent": {"intent": "track two bounded items"},
                    "allowed_mutation_types": ["item_create"],
                    "planned_create_types": ["item"],
                }
                if include_scope
                else None
            ),
            "patch": {
                "operations": [
                    {
                        "operation": "action_upsert",
                        "action": {
                            "mutation_type": "item_create",
                            "change_id": change_id,
                            "action": "create",
                            "object_type": "item",
                            "affected_fields": ["title", "kind"],
                            "basis_refs": [utterance.ref_id],
                            "create_spec": {
                                "title": title,
                                "kind": "verification.item",
                            },
                        },
                    }
                ]
            },
        }
    )


def _stage(
    factory: sessionmaker[Session],
    *,
    utterance_ref: str,
    token: str,
    argument_hash: str,
    request: StageChangesInput,
) -> dict[str, Any]:
    with factory.begin() as session:
        return ChangeSetAssemblyService(session).stage(
            request,
            assembly_operation_token=token,
            assembly_argument_hash=argument_hash,
        )


def _create_utterance(
    factory: sessionmaker[Session], message_id: str, text: str
) -> tuple[str, str]:
    with factory.begin() as session:
        utterance = _utterance(message_id, text)
        session.add(utterance)
        session.flush()
        return utterance.ref_id, utterance.request_key


def _admit_committed(
    factory: sessionmaker[Session],
    *,
    utterance_ref: str,
    trace_ref: str,
    call_id: str,
    ordinal: int,
    tool_name: str,
    argument_hash: str,
) -> str:
    with factory.begin() as session:
        return _admit(
            session,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id=call_id,
            ordinal=ordinal,
            tool_name=tool_name,
            argument_hash=argument_hash,
        )


def _review(
    factory: sessionmaker[Session],
    *,
    utterance_ref: str,
    request_key: str,
    trace_ref: str,
    call_id: str,
    ordinal: int,
    argument_hash: str,
    view: Literal["summary", "diff"] = "summary",
    cursor: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_ref,
        call_id=call_id,
        ordinal=ordinal,
        tool_name="docket_review_changeset",
        argument_hash=argument_hash,
    )
    with factory.begin() as session:
        return ChangeSetAssemblyService(session).review(
            ReviewChangesInput(
                utterance_ref=utterance_ref,
                request_key=request_key,
                view=view,
                cursor=cursor,
                limit=limit,
            ),
            assembly_operation_token=token,
            assembly_argument_hash=argument_hash,
        )


def test_same_attempt_concurrent_calls_bind_old_revision(
    factory: sessionmaker[Session],
) -> None:
    utterance_ref, _request_key = _create_utterance(
        factory,
        "1542799000000000911",
        "Track these two PostgreSQL race-test items.",
    )
    trace_ref = new_public_ref("trace")
    first_hash = "1" * 64
    stale_hash = "2" * 64
    with factory.begin() as session:
        first_token = _admit(
            session,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id="same-attempt-first",
            ordinal=1,
            tool_name="docket_stage_changes",
            argument_hash=first_hash,
        )
        stale_token = _admit(
            session,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id="same-attempt-stale",
            ordinal=2,
            tool_name="docket_stage_changes",
            argument_hash=stale_hash,
        )
        utterance = _load_utterance(session, utterance_ref)
        first_request = _item_stage(
            utterance,
            change_id="same-attempt-a",
            title="Same attempt A",
            include_scope=True,
        )
        stale_request = _item_stage(
            utterance,
            change_id="same-attempt-b",
            title="Same attempt B",
            include_scope=False,
        )
    first = _stage(
        factory,
        utterance_ref=utterance_ref,
        token=first_token,
        argument_hash=first_hash,
        request=first_request,
    )
    assert first["current_revision"] == 1
    stale = _stage(
        factory,
        utterance_ref=utterance_ref,
        token=stale_token,
        argument_hash=stale_hash,
        request=stale_request,
    )
    assert stale["disposition"] == "draft_revision_conflict"
    assert stale["error"]["details"] == {
        "observed_revision": None,
        "current_revision": 1,
    }


def test_cross_attempt_stale_edit_and_commit_are_rejected(
    factory: sessionmaker[Session],
) -> None:
    utterance_ref, request_key = _create_utterance(
        factory,
        "1542799000000000912",
        "Track these items across two execution attempts.",
    )
    trace_a = new_public_ref("trace")
    trace_b = new_public_ref("trace")
    first_hash = "3" * 64
    first_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_a,
        call_id="cross-first",
        ordinal=1,
        tool_name="docket_stage_changes",
        argument_hash=first_hash,
    )
    with factory() as session:
        first_request = _item_stage(
            _load_utterance(session, utterance_ref),
            change_id="cross-a",
            title="Cross attempt A",
            include_scope=True,
        )
    assert (
        _stage(
            factory,
            utterance_ref=utterance_ref,
            token=first_token,
            argument_hash=first_hash,
            request=first_request,
        )["current_revision"]
        == 1
    )
    assert (
        _review(
            factory,
            utterance_ref=utterance_ref,
            request_key=request_key,
            trace_ref=trace_b,
            call_id="cross-review",
            ordinal=1,
            argument_hash="4" * 64,
        )["revision"]
        == 1
    )

    with factory() as session:
        second_request = _item_stage(
            _load_utterance(session, utterance_ref),
            change_id="cross-b",
            title="Cross attempt B",
            include_scope=False,
        )
    second_hash = "5" * 64
    second_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_a,
        call_id="cross-second",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash=second_hash,
    )
    assert (
        _stage(
            factory,
            utterance_ref=utterance_ref,
            token=second_token,
            argument_hash=second_hash,
            request=second_request,
        )["current_revision"]
        == 2
    )

    stale_stage_hash = "6" * 64
    stale_stage_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_b,
        call_id="cross-stale-stage",
        ordinal=2,
        tool_name="docket_stage_changes",
        argument_hash=stale_stage_hash,
    )
    stale_stage = _stage(
        factory,
        utterance_ref=utterance_ref,
        token=stale_stage_token,
        argument_hash=stale_stage_hash,
        request=second_request,
    )
    assert stale_stage["disposition"] == "draft_revision_conflict"
    assert stale_stage["error"]["details"] == {
        "observed_revision": 1,
        "current_revision": 2,
    }

    commit_hash = "7" * 64
    commit_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_b,
        call_id="cross-stale-commit",
        ordinal=3,
        tool_name="docket_commit_changeset",
        argument_hash=commit_hash,
    )
    with factory.begin() as session:
        stale_commit = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref,
            request_key=request_key,
            assembly_operation_token=commit_token,
            assembly_argument_hash=commit_hash,
        )
    assert stale_commit["disposition"] == "draft_revision_conflict"


def test_one_changeset_lineage_per_semantic_request(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        existing = session.scalar(select(ChangeSet).where(ChangeSet.current_revision >= 1).limit(1))
        assert existing is not None and existing.semantic_request_ref is not None
        duplicate = ChangeSet(
            intent_session_id=existing.intent_session_id,
            intent_session_ref=existing.intent_session_ref,
            semantic_request_ref=existing.semantic_request_ref,
            authority_scope_hash=existing.authority_scope_hash,
            precondition_hash=existing.precondition_hash,
            execution_binding_json={"kind": "postgres_constraint_probe"},
            idempotency_key=f"constraint-probe:{existing.semantic_request_ref}",
            state="draft",
            version=1,
            current_revision=1,
        )
        session.add(duplicate)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("PostgreSQL accepted two ChangeSets for one semantic request")


def _schedule_stage(
    utterance: OperatorUtterance,
    *,
    source_ref: str,
    lane_ref: str,
    start_index: int,
    count: int,
    include_scope: bool,
) -> StageChangesInput:
    base = datetime(2026, 9, 8, 9, 0)
    operations: list[dict[str, Any]] = []
    for index in range(start_index, start_index + count):
        start = base + timedelta(days=index)
        end = start + timedelta(minutes=50)
        operations.append(
            {
                "operation": "normalized_entry_upsert",
                "entry": {
                    "entry_type": "scheduled_occurrence_entry",
                    "import_entry_id": f"postgres-schedule-{index:02d}",
                    "evidence": {
                        "source_ref": source_ref,
                        "source_fragment_locator": {"page": 1, "cell": index},
                        "source_fragment_hash": hashlib.sha256(
                            f"postgres-matrix-cell-{index}".encode()
                        ).hexdigest(),
                        "extractor_identifier": "docket.postgres-smoke",
                        "extractor_version": "1",
                    },
                    "title": f"MATH 1263 — PostgreSQL topic {index + 1}",
                    "kind": "academic.lecture_topic",
                    "lane_ref": lane_ref,
                    "timing": {
                        "kind": "timed",
                        "start_local": start.isoformat(),
                        "end_local": end.isoformat(),
                        "timezone": "America/Los_Angeles",
                    },
                },
            }
        )
    return StageChangesInput.model_validate(
        {
            "utterance_ref": utterance.ref_id,
            "request_key": utterance.request_key,
            "assembly_scope": (
                {
                    "resolved_intent": {"intent": "verify thirty-entry PostgreSQL assembly"},
                    "normalized_entry_types": ["scheduled_occurrence_entry"],
                    "target_refs": [lane_ref],
                    "source_refs": [source_ref],
                }
                if include_scope
                else None
            ),
            "patch": {"operations": operations},
        }
    )


def test_thirty_entry_schedule_commits_once(factory: sessionmaker[Session]) -> None:
    import docket.services.changeset_assembly as assembly_module
    import docket.services.changeset_pins as pins_module
    from docket.services.change_sets import ChangeSetService

    compiler = assembly_module.compile_normalized_entry

    def faulty_compiler(entry: Any, **kwargs: Any) -> Any:
        if entry.import_entry_id == "postgres-schedule-17":
            raise DocketError(code="injected_compiler_fault", message="Synthetic failure")
        return compiler(entry, **kwargs)

    with factory.begin() as session:
        utterance = _utterance(
            "1542799000000000913",
            "Import these thirty exact schedule entries in one atomic ChangeSet.",
        )
        source = Source(
            source_kind="attachment",
            external_ref="discord-attachment:postgres-schedule",
            observed_at=datetime.now(UTC),
            content_hash=hashlib.sha256(b"postgres-schedule").hexdigest(),
            metadata_json={},
        )
        account = ProviderAccount(
            provider="google",
            external_account_id="postgres-assembly-smoke",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add_all([source, account])
        session.flush()
        utterance.attachment_source_refs = [source.ref_id]
        session.add(utterance)
        session.flush()
        session.add(
            AttachmentEvidence(
                ref_id=source.ref_id,
                transport="discord",
                transport_attachment_ref="postgres-schedule",
                source_message_ref=utterance.source_message_ref,
                operator_utterance_ref=utterance.ref_id,
                filename="postgres-schedule.png",
                media_type="image/png",
                byte_size=4096,
                content_hash=source.content_hash,
                received_at=utterance.said_at,
                ingest_state="available",
                retention_disposition="derived_only",
            )
        )
        lane = CalendarLane(
            account_id=account.id,
            lane="postgres-math-1263",
            display_name="PostgreSQL MATH 1263",
            color_hex="#3367D6",
            calendar_id="postgres-math-1263@example.com",
            status="active",
            basis_refs=[utterance.ref_id],
            created_by_changeset_ref="chg_01M1A100000000000000000000",
        )
        session.add(lane)
        session.flush()
        utterance_ref = utterance.ref_id
        request_key = utterance.request_key
        source_ref = source.ref_id
        lane_ref = lane.ref_id

    trace_ref = new_public_ref("trace")
    for batch, start_index in enumerate((0, 15), start=1):
        argument_hash = str(batch + 7) * 64
        token = _admit_committed(
            factory,
            utterance_ref=utterance_ref,
            trace_ref=trace_ref,
            call_id=f"postgres-schedule-{batch}",
            ordinal=batch,
            tool_name="docket_stage_changes",
            argument_hash=argument_hash,
        )
        with factory() as session:
            request = _schedule_stage(
                _load_utterance(session, utterance_ref),
                source_ref=source_ref,
                lane_ref=lane_ref,
                start_index=start_index,
                count=15,
                include_scope=batch == 1,
            )
        with (
            patch.object(assembly_module, "compile_normalized_entry", faulty_compiler)
            if batch == 2
            else nullcontext()
        ):
            result = _stage(
                factory,
                utterance_ref=utterance_ref,
                token=token,
                argument_hash=argument_hash,
                request=request,
            )
        assert result["disposition"] == ("ready_to_commit" if batch == 1 else "saved_with_errors")
        assert len(json.dumps(result, separators=(",", ":")).encode()) < 16 * 1024

    with factory() as session:
        draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == result["draft_ref"]))
        assert draft is not None and draft.state == "draft"
        assert len(draft.normalized_entries_json) == 30
        assert draft.staged_actions_json is not None and len(draft.staged_actions_json) == 116
        revision = session.scalar(
            select(ChangeSetRevision).where(
                ChangeSetRevision.change_set_id == draft.id,
                ChangeSetRevision.revision == 2,
            )
        )
        assert revision is not None and len(revision.normalized_entries_json) == 30
        assert revision.staged_actions_json == draft.staged_actions_json
        revision_id = revision.id
        repair = _schedule_stage(
            _load_utterance(session, utterance_ref),
            source_ref=source_ref,
            lane_ref=lane_ref,
            start_index=17,
            count=1,
            include_scope=False,
        )
    try:
        with factory.begin() as session:
            session.execute(
                text("UPDATE change_set_revisions SET staged_actions_json = '[]' WHERE id = :id"),
                {"id": revision_id},
            )
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL allowed rewriting immutable failed-draft inputs")
    repair_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_ref,
        call_id="repair-schedule",
        ordinal=3,
        tool_name="docket_stage_changes",
        argument_hash="e" * 64,
    )
    repaired = _stage(
        factory,
        utterance_ref=utterance_ref,
        token=repair_token,
        argument_hash="e" * 64,
        request=repair,
    )
    assert repaired["disposition"] == "ready_to_commit", repaired
    assert repaired["normalized_entry_count"] == 30

    with factory() as session:
        draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == repaired["draft_ref"]))
        assert draft is not None
        execution_pin = draft.compiler_manifest_json["execution_pin"]
        assert len(execution_pin["normalized_entry_compilers"]) == 30
        assert execution_pin["compiled_effect_hash"]
    try:
        with factory.begin() as session:
            session.execute(
                text(
                    "UPDATE change_set_revisions SET compiler_manifest_json = '{}' WHERE id = :id"
                ),
                {"id": revision_id},
            )
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL allowed rewriting an immutable execution pin")

    commit_hash = "f" * 64
    commit_token = _admit_committed(
        factory,
        utterance_ref=utterance_ref,
        trace_ref=trace_ref,
        call_id="postgres-schedule-commit",
        ordinal=4,
        tool_name="docket_commit_changeset",
        argument_hash=commit_hash,
    )
    with (
        factory.begin() as session,
        patch.object(pins_module, "COMPILER_VERSION", 2),
        patch.object(assembly_module, "COMPILER_VERSION", 3),
        patch.object(
            assembly_module,
            "compile_normalized_entry",
            side_effect=AssertionError("Resumed commit reran the entry compiler"),
        ),
        patch.object(
            ChangeSetService,
            "_compile_required_provider_intents",
            side_effect=AssertionError("Resumed commit reran the provider compiler"),
        ),
    ):
        result = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref,
            request_key=request_key,
            assembly_operation_token=commit_token,
            assembly_argument_hash=commit_hash,
        )
        committed = session.scalar(
            select(ChangeSet).where(ChangeSet.ref_id == repaired["draft_ref"])
        )
        assert committed is not None
        assert committed.compiler_manifest_json["execution_pin"] == execution_pin
    assert result["disposition"] == "committed", result
    assert result["canonical_effect_count"] == 120
    assert result["provider_operation_count"] == 30
    assert len(json.dumps(result, separators=(",", ":")).encode()) < 16 * 1024
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(CanonicalEvent.id)).where(
                    CanonicalEvent.created_by_changeset_ref == result["changeset_ref"]
                )
            )
            == 30
        )
        assert (
            session.scalar(
                select(func.count(Operation.id)).where(
                    Operation.originating_changeset_ref == result["changeset_ref"]
                )
            )
            == 30
        )

    # Exercise real PostgreSQL ownership locking around a lost provider response.
    # The remote endpoint here is only a stateful fake, never Google.
    provider = FakeCalendarProvider()
    original_runner = OperationRunner(factory, provider)
    original = original_runner.claim_due()
    assert original is not None
    with factory() as session:
        operation = session.get(Operation, original.operation_id)
        assert (
            operation is not None and operation.originating_changeset_ref == result["changeset_ref"]
        )
    original_runner.mark_provider_call_started(original)
    remote_result = provider.create_event(original.calendar_request())
    with factory.begin() as session:
        operation = session.get(Operation, original.operation_id)
        target = session.get(OperationTarget, original.operation_target_id)
        assert operation is not None and target is not None
        operation.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        target.leased_until = operation.leased_until
    resumed_runner = OperationRunner(factory, provider)
    assert resumed_runner.recover_expired_leases() == 1
    resumed = resumed_runner.claim_reconciliation()
    assert resumed is not None and resumed.operation_id == original.operation_id
    barrier = threading.Barrier(2)

    def late_original() -> None:
        barrier.wait(timeout=10)
        original_runner._finish_error(
            original,
            CalendarProviderError("late_callback", "Synthetic late failure", transient=False),
        )

    def reconcile_current() -> None:
        barrier.wait(timeout=10)
        matches = provider.find_by_correlation(resumed.calendar_request())
        assert len(matches) == 1
        resumed_runner._finish_event_success(resumed, matches[0])

    with ThreadPoolExecutor(max_workers=2) as pool:
        late = pool.submit(late_original)
        current = pool.submit(reconcile_current)
        late.result(timeout=20)
        current.result(timeout=20)
    with factory() as session:
        operation = session.get(Operation, original.operation_id)
        prior_attempt = session.get(ExecutionAttempt, original.attempt_id)
        current_attempt = session.get(ExecutionAttempt, resumed.attempt_id)
        assert operation is not None and operation.status == "succeeded"
        assert operation.result["provider_event_id"] == remote_result.external_event_id
        assert prior_attempt is not None and prior_attempt.status == "unknown"
        assert current_attempt is not None and current_attempt.status == "succeeded"
    assert len(provider.events) == 1

    # Counts and per-target rows use the same PostgreSQL statement snapshot.
    # This is status only: the remaining 29 queued deliveries are not retried.
    with factory() as session:
        page = HistoryService(session).get_entry(result["changeset_ref"], view="delivery", limit=3)
        assert page["provider_operation_count"] == 30
        assert page["provider_state_counts"] == {"queued": 29, "confirmed": 1}
        assert page["count"] == 3 and page["omitted_target_count"] == 27
        assert page["cursor"]
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 16 * 1024
        first_refs = {item["operation_ref"] for item in page["items"]}
    with factory() as session:
        following = HistoryService(session).get_entry(
            result["changeset_ref"], view="delivery", limit=3, cursor=page["cursor"]
        )
        assert following["provider_state_counts"] == {"queued": 29, "confirmed": 1}
        assert not first_refs.intersection(item["operation_ref"] for item in following["items"])


def test_diff_pages_keep_both_revisions_across_connections(factory: sessionmaker[Session]) -> None:
    utterance_ref, request_key = _create_utterance(
        factory, "1542799000000000919", "Track a test item for revision-bound diff verification."
    )
    trace_a, trace_b = new_public_ref("trace"), new_public_ref("trace")

    def stage(trace: str, ordinal: int, title: str, include_scope: bool) -> None:
        digest = hashlib.sha256(title.encode()).hexdigest()
        token = _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace,
            call_id=f"diff-stage-{title}", ordinal=ordinal,
            tool_name="docket_stage_changes", argument_hash=digest,
        )
        with factory() as session:
            request = _item_stage(
                _load_utterance(session, utterance_ref), change_id="diff-item",
                title=title, include_scope=include_scope,
            )
        assert _stage(
            factory, utterance_ref=utterance_ref, token=token,
            argument_hash=digest, request=request,
        )["disposition"] == "ready_to_commit"

    stage(trace_a, 1, "Original diff title", True)
    page = _review(
        factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_a,
        call_id="diff-first", ordinal=2, argument_hash="a" * 64, view="diff", limit=1,
    )
    assert page["revision"] == 1 and page["truncated"]
    rows = list(page["items"])
    _review(
        factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_b,
        call_id="diff-observe-b", ordinal=1, argument_hash="b" * 64,
    )
    stage(trace_b, 2, "New concurrent title", False)
    ordinal = 2
    while page.get("cursor"):
        ordinal += 1
        page = _review(
            factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_a,
            call_id=f"diff-page-{ordinal}", ordinal=ordinal,
            argument_hash=hashlib.sha256(str(ordinal).encode()).hexdigest(),
            view="diff", limit=1, cursor=page["cursor"],
        )
        assert page["revision"] == 1 and page["current_revision"] == 2
        assert page["base_revision"] is None
        rows.extend(page["items"])
    assert len(rows) == page["total_if_known"]
    titles = [row["after"] for row in rows if row["field_path"] == ["create_spec", "title"]]
    assert titles == ["Original diff title"]
    token = _admit_committed(
        factory, utterance_ref=utterance_ref, trace_ref=trace_a, call_id="diff-stale-commit",
        ordinal=ordinal + 1, tool_name="docket_commit_changeset", argument_hash="c" * 64,
    )
    with factory.begin() as session:
        result = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=token, assembly_argument_hash="c" * 64,
        )
        assert result["disposition"] == "draft_revision_conflict"
        assert result["error"]["details"]["observed_revision"] == 1


def test_invocation_binding_transport_retries_serialize(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        utterance = _utterance("1542799000000000679", "Read this smoke context.")
        session.add(utterance)
        session.flush()
        utterance_ref = utterance.ref_id
    trace_ref = new_public_ref("trace")
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "format": 1, "trace_ref": trace_ref, "call_id": "same-transport-call", "ordinal": 1,
        "utterance_ref": utterance_ref, "gateway_instance_ref": None,
        "tool_name": "docket_search_history", "argument_hash": sha256_json({}),
        "contract_version": CONTRACT_VERSION, "contract_hash": contract_hash("interactive"),
        "issued_at": now, "expires_at": now + 900,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).decode().rstrip("=")
    signature = hmac.new(
        get_settings().hermes_to_docket_token().encode(),
        b"docket-mcp-invocation-v1:" + encoded.encode(), hashlib.sha256,
    ).hexdigest()
    barrier = threading.Barrier(2)

    def dispatch(_index: int) -> None:
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            invocation = ToolInvocation(
                tool_name="docket_search_history", tool_contract_version=CONTRACT_VERSION,
                caller_profile="interactive", received_argument_hash=sha256_json({}),
                transport_state="completed", completed_at=datetime.now(UTC),
            )
            session.add(invocation)
            session.flush()
            barrier.wait(timeout=10)
            bind_invocation(session, invocation, f"{encoded}.{signature}", arguments={})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch, (0, 1)))
    with factory() as session:
        invocations = list(session.scalars(select(ToolInvocation).where(
            ToolInvocation.trace_ref == trace_ref
        )))
        assert len(invocations) == 2
        assert {item.trace_call_id for item in invocations} == {"same-transport-call", None}
        assert all(item.utterance_refs == [utterance_ref] for item in invocations)
        assert all(item.trace_ordinal == 1 for item in invocations)


def main() -> None:
    database_url = os.environ["DOCKET_DATABASE_URL"]
    engine = configure_database(database_url)
    assert engine.dialect.name == "postgresql"
    factory = sessionmaker(engine, expire_on_commit=False)
    checks = (
        test_same_attempt_concurrent_calls_bind_old_revision,
        test_cross_attempt_stale_edit_and_commit_are_rejected,
        test_one_changeset_lineage_per_semantic_request,
        test_thirty_entry_schedule_commits_once,
        test_native_and_deferred_ingress_claim_once,
        test_relative_date_capture_serializes,
        test_occurrence_commits_serialize_and_identity_is_immutable,
        test_diff_pages_keep_both_revisions_across_connections,
        test_invocation_binding_transport_retries_serialize,
    )
    for check in checks:
        check(factory)
    engine.dispose()
    print(
        json.dumps(
            {
                "status": "passed",
                "database": "postgresql",
                "checks": [check.__name__ for check in checks],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

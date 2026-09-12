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
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import Any, Literal
from unittest.mock import patch

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from trace_execution_checks import test_retained_trace_migration_round_trip

from docket.config import get_settings
from docket.database import configure_database
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import (
    AttachmentManifest,
    GatewayAgentResponseCapture,
    McpTraceCallUpdate,
    McpTraceCheckpoint,
    McpTraceUpdate,
    OperatorUtteranceCapture,
    TraceTimingInput,
)
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.models import (
    AgentResponse,
    AssemblyExecution,
    AssemblyOperation,
    AttachmentEvidence,
    AuditEvent,
    CalendarDateBinding,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ChangeSetRevision,
    ConversationalToolTrace,
    DeferredIngress,
    EventOccurrence,
    ExecutionAttempt,
    ExecutionLease,
    GatewayLifetime,
    IntentTurn,
    Item,
    LaneRoutingDecision,
    Operation,
    OperationTarget,
    OperatorUtterance,
    ProviderAccount,
    ProviderEventBinding,
    RequestAssemblyAdoption,
    RequestEntryInterpretation,
    SemanticRequest,
    SemanticRequestAttempt,
    SemanticRequestSpecification,
    Source,
    Task,
    ToolInvocation,
    TraceExecutionSegment,
    TraceTimingObservation,
)
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.authority import ChangeSetContent, IntentSessionOpen, IntentTurnAppend
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.attachment_evidence import AttachmentEvidenceService, AttachmentTextService
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.continuity import ContinuityService
from docket.services.event_occurrences import (
    bind_calendar_date,
    identity_for_timing,
    occurrence_timing,
)
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.history import HistoryService
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.invocation_binding import bind_invocation
from docket.services.mcp_traces import McpTraceService
from docket.services.operations import OperationRunner
from docket.services.provenance import ProvenanceService
from docket.services.trace_executions import TraceExecutionService
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
    staged_draft_refs: list[str] = []
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
        assert staged["event_effect_count"] == 1
        assert staged["event_preview"][0]["scope"]["kind"] == "occurrence"
        staged_draft_refs.append(staged["draft_ref"])
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
        for revision in session.scalars(
            select(ChangeSetRevision).join(ChangeSet).where(ChangeSet.ref_id.in_(staged_draft_refs))
        ):
            preview = revision.compiler_manifest_json["canonical_event_preview"]
            assert preview["effect_count"] == 1
            effect = preview["effects"][0]
            assert effect["before"]["status"] == "active"
            assert effect["after"]["status"] == "cancelled"
            assert effect["before"]["timing"]["start_local"] == "2026-09-08T15:00:00"
            assert effect["observed_version"] == 1 < series.version
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
    try:
        with factory.begin() as session:
            session.execute(text(
                "UPDATE changeset_revisions SET compiler_manifest_json='{}'::json "
                "WHERE change_set_id IN (SELECT id FROM changesets WHERE ref_id=:ref)"
            ), {"ref": staged_draft_refs[0]})
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL permitted rewriting a pinned canonical preview")


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
            registration_key=uuid.uuid4(), instance_kind="ingress_claim_smoke"
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


def _bind_execution(session, utterance, *, label=None, started_at=None, gateway=None, token=None):
    session.scalar(select(OperatorUtterance).where(
        OperatorUtterance.ref_id == utterance.ref_id,
    ).with_for_update())
    """Synthetic admitted execution; its label never replaces the server binding."""
    settings = get_settings()
    label = label or uuid.uuid4().hex
    key = f"test:trace:{utterance.ref_id}:{label}"
    lease = session.scalar(select(ExecutionLease).where(
        ExecutionLease.completion_token == token if token else ExecutionLease.lease_key == key,
    ))
    if lease is None:
        if gateway is None:
            live = session.scalar(select(GatewayLifetime).where(
                GatewayLifetime.instance_kind == "hermes_discord_gateway",
                GatewayLifetime.status == "active",
            ))
            gateway = live.ref_id if live else GatewayLifetimeService(session).register(
                registration_key=uuid.uuid4(), instance_kind="hermes_discord_gateway",
            )["ref"]
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key=key, lease_kind="interactive_turn", subject_ref=utterance.ref_id,
            gateway_instance_ref=gateway,
        )
    start = started_at or datetime.now(UTC)
    parts = utterance.source_message_ref.split(":")
    if label.startswith("trace_") and session.scalar(select(ConversationalToolTrace).where(
        ConversationalToolTrace.guild_id == parts[1],
        ConversationalToolTrace.source_channel_id == parts[2],
        ConversationalToolTrace.source_message_id == parts[3],
    )) is None:
        # Stable synthetic first-ref fixtures only. Production always lets Docket
        # allocate it; the resumed execution still receives this same parent.
        session.add(ConversationalToolTrace(
            ref_id=label, guild_id=parts[1], source_channel_id=parts[2],
            source_message_id=parts[3], actor_id=settings.operator_discord_user_id,
            started_at=start, version=0,
        ))
        session.flush()
    result = TraceExecutionService(session).bind(
        utterance_ref=utterance.ref_id, execution_completion_token=lease.completion_token,
        gateway_instance_ref=lease.gateway_instance_ref, turn_started_at=start,
        tool_contract_version=CONTRACT_VERSION, tool_contract_hash=contract_hash("interactive"),
    )
    segment = session.scalar(select(TraceExecutionSegment).where(
        TraceExecutionSegment.execution_lease_id == lease.id,
    ))
    return {
        "trace_ref": result["trace_ref"], "execution_index": result["execution_index"],
        "execution_completion_token": lease.completion_token,
        "gateway_instance_ref": lease.gateway_instance_ref,
        "turn_started_at": datetime.fromisoformat(result["turn_started_at"]),
        "trace_execution_id": segment.id,
    }


def _callback_binding(binding):
    return {key: binding[key] for key in (
        "execution_index", "execution_completion_token", "gateway_instance_ref", "turn_started_at",
    )}


def _segment_for(session, ref, index=1):
    return session.scalar(select(TraceExecutionSegment).where(
        TraceExecutionSegment.trace_ref == ref, TraceExecutionSegment.execution_index == index,
    ))


def _admit(
    session: Session,
    *,
    utterance_ref: str,
    trace_ref: str,
    call_id: str,
    ordinal: int,
    tool_name: str,
    argument_hash: str,
    execution_binding: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    utterance = _load_utterance(session, utterance_ref)
    binding = execution_binding or _bind_execution(session, utterance, label=trace_ref)
    admitted = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id,
        trace_ref=binding["trace_ref"],
        **{key: binding[key] for key in (
            "execution_index", "execution_completion_token", "gateway_instance_ref",
        )},
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


def test_cold_restart_reuses_one_trace_and_staged_request(factory: sessionmaker[Session]) -> None:
    utterance_ref, request_key = _create_utterance(
        factory, "1542799000000000798", "Track the cold-restart verification item."
    )
    kind = "trace_restart_smoke"
    with factory.begin() as session:
        gateway = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(), instance_kind=kind,
        )["ref"]
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key=f"trace-cold-first:{utterance_ref}", lease_kind="interactive_turn",
            subject_ref=utterance_ref, gateway_instance_ref=gateway,
        )
        binding_args = {
            "utterance_ref": utterance_ref, "execution_completion_token": lease.completion_token,
            "gateway_instance_ref": gateway, "turn_started_at": datetime.now(UTC),
            "tool_contract_version": CONTRACT_VERSION,
            "tool_contract_hash": contract_hash("interactive"),
        }
    barrier = threading.Barrier(2)

    def bind_first():
        barrier.wait(timeout=10)
        with factory.begin() as session:
            return TraceExecutionService(session).bind(**binding_args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: bind_first(), range(2)))
    assert {row["disposition"] for row in results} == {"bound", "replayed_request"}
    assert results[0]["trace_ref"] == results[1]["trace_ref"]
    assert results[0]["execution_index"] == results[1]["execution_index"] == 1
    trace_ref = results[0]["trace_ref"]
    first = {**binding_args, **results[0]}
    with factory.begin() as session:
        utterance = _load_utterance(session, utterance_ref)
        token = _admit(
            session, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="same-upstream",
            ordinal=1, tool_name="docket_stage_changes", argument_hash="a" * 64,
            execution_binding=first,
        )
        receipt = ChangeSetAssemblyService(session).stage(
            _item_stage(
                utterance, change_id="restart-item", title="Cold-restart item", include_scope=True
            ),
            assembly_operation_token=token,
            assembly_argument_hash="a" * 64,
        )
        assert receipt["disposition"] == "ready_to_commit"
        draft = session.scalar(select(ChangeSet).join(SemanticRequest,
            ChangeSet.semantic_request_ref == SemanticRequest.ref_id).where(
                SemanticRequest.origin_utterance_refs[0].as_string() == utterance_ref,
            ))
        draft_ref, request_ref = draft.ref_id, draft.semantic_request_ref
    with factory.begin() as session:
        lifetime = session.scalar(select(GatewayLifetime).where(GatewayLifetime.ref_id == gateway))
        lifetime.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
    with factory.begin() as session:
        replacement = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(), instance_kind=kind,
        )["ref"]
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key=f"trace-cold-second:{utterance_ref}", lease_kind="interactive_turn",
            subject_ref=utterance_ref, gateway_instance_ref=replacement,
        )
        resumed_args = {
            **binding_args,
            "gateway_instance_ref": replacement,
            "execution_completion_token": lease.completion_token,
            "turn_started_at": datetime.now(UTC),
        }
        resumed = {**resumed_args, **TraceExecutionService(session).bind(**resumed_args)}
        assert resumed["trace_ref"] == trace_ref and resumed["execution_index"] == 2
        review_token = _admit(
            session, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="same-upstream",
            ordinal=1, tool_name="docket_review_changeset", argument_hash="b" * 64,
            execution_binding=resumed,
        )
        assert review_token != token
        ChangeSetAssemblyService(session).review(
            ReviewChangesInput(utterance_ref=utterance_ref, request_key=request_key),
            assembly_operation_token=review_token, assembly_argument_hash="b" * 64,
        )
    with factory.begin() as session:
        commit_token = _admit(
            session, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="commit",
            ordinal=2, tool_name="docket_commit_changeset", argument_hash="c" * 64,
            execution_binding=resumed,
        )
    for _ in range(2):
        with factory.begin() as session:
            committed = ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=commit_token, assembly_argument_hash="c" * 64,
            )
            assert committed["canonical_disposition"] == "committed"
    try:
        with factory.begin() as session:
            TraceExecutionService(session).require(
                trace_ref=trace_ref, execution_index=1,
                execution_completion_token=first["execution_completion_token"],
                gateway_instance_ref=gateway,
            )
    except DocketError as exc:
        assert exc.code == "gateway_lifetime_fenced"
    else:
        raise AssertionError("An old gateway changed a resumed execution")
    with factory() as session:
        assert session.scalar(select(func.count(ConversationalToolTrace.id)).where(
            ConversationalToolTrace.ref_id == trace_ref,
        )) == 1
        segments = list(session.scalars(select(TraceExecutionSegment).where(
            TraceExecutionSegment.trace_ref == trace_ref,
        ).order_by(TraceExecutionSegment.execution_index)))
        assert [row.status for row in segments] == ["interrupted", "running"]
        assert session.scalar(select(func.count(AssemblyExecution.id)).where(
            AssemblyExecution.trace_ref == trace_ref,
        )) == 2
        assert session.scalar(select(func.count(SemanticRequestAttempt.id)).where(
            SemanticRequestAttempt.semantic_request_ref == request_ref,
        )) == 2
        assert session.scalar(select(func.count(ChangeSet.id)).where(
            ChangeSet.semantic_request_ref == request_ref, ChangeSet.ref_id == draft_ref,
            ChangeSet.state == "committed",
        )) == 1
        assert (
            session.scalar(select(func.count(Item.id)).where(Item.title == "Cold-restart item"))
            == 1
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


def test_pre_admitted_initial_stages_share_one_request(factory: sessionmaker[Session]) -> None:
    utterance_ref, _request_key = _create_utterance(
        factory, "1542799000000000686", "Track the initial-stage concurrency fixture.",
    )
    inputs = []
    for index in range(2):
        trace_ref = new_public_ref("trace")
        digest = str(index + 1) * 64
        token = _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace_ref,
            call_id=f"initial-race-{index}", ordinal=1, tool_name="docket_stage_changes",
            argument_hash=digest,
        )
        with factory.begin() as session:
            request = _item_stage(
                _load_utterance(session, utterance_ref), change_id="same-item",
                title=f"Interpretation {index}", include_scope=True,
            )
        inputs.append((token, digest, request))
    barrier = threading.Barrier(2)

    def execute(values: tuple[str, str, StageChangesInput]) -> dict[str, Any]:
        token, digest, request = values
        barrier.wait(timeout=10)
        return _stage(
            factory, utterance_ref=utterance_ref, token=token,
            argument_hash=digest, request=request,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, values) for values in inputs]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(result["disposition"] for result in results) == [
        "draft_revision_conflict", "ready_to_commit",
    ]
    with factory.begin() as session:
        requests = [row for row in session.scalars(select(SemanticRequest))
                    if utterance_ref in row.origin_utterance_refs]
        assert len(requests) == 1
        draft = session.scalar(select(ChangeSet).where(
            ChangeSet.semantic_request_ref == requests[0].ref_id,
        ))
        assert draft is not None and draft.current_revision == 1
        assert session.scalar(select(func.count(ChangeSetRevision.id)).where(
            ChangeSetRevision.change_set_id == draft.id,
        )) == 1
        attempts = list(session.scalars(select(SemanticRequestAttempt).where(
            SemanticRequestAttempt.semantic_request_id == requests[0].id,
        )))
        assert sorted(attempt.attempt_number for attempt in attempts) == [1, 2]


def test_direct_receipt_resume_admissions_serialize(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        utterance = _utterance("1542799000000000687", "Track the receipt recovery fixture.")
        session.add(utterance)
        session.flush()
        staged = _item_stage(utterance, change_id="receipt-item", title="Receipt fixture",
                             include_scope=True)
        outcome = InteractiveAuthorityService(session).process_turn(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            actor_id=str(get_settings().operator_discord_user_id), intent_session_ref=None,
            expected_session_version=None, statements=[], relations=[],
            resolved_intent_json={"kind": "receipt_fixture"}, blocking_clarifications=[],
            content=ChangeSetContent(
                basis_refs=[utterance.ref_id],
                tracked_context_changes=[staged.patch.operations[0].action],
            ), changeset_ref=None, expected_changeset_version=None,
        )
        assert outcome["state"] == "committed"
        request_ref = outcome["semantic_request_ref"]
        changeset = session.scalar(select(ChangeSet).where(
            ChangeSet.semantic_request_ref == request_ref,
        ))
        assert changeset is not None
        receipt = deepcopy(changeset.commit_receipt_json)
        utterance_ref, request_key = utterance.ref_id, utterance.request_key
        original_item_count = session.scalar(select(func.count(Item.id)))
    barrier = threading.Barrier(2)

    def resume(index: int) -> dict[str, Any]:
        barrier.wait(timeout=10)
        digest = str(index + 3) * 64
        token = _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=new_public_ref("trace"),
            call_id=f"direct-resume-{index}", ordinal=1, tool_name="docket_commit_changeset",
            argument_hash=digest,
        )
        with factory.begin() as session:
            return ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=token, assembly_argument_hash=digest,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume, index) for index in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results == [receipt, receipt]
    with factory.begin() as session:
        assert session.scalar(select(func.count(Item.id))) == original_item_count
        assert session.scalar(select(func.count(ChangeSet.id)).where(
            ChangeSet.semantic_request_ref == request_ref,
        )) == 1
        attempts = list(session.scalars(select(SemanticRequestAttempt).where(
            SemanticRequestAttempt.semantic_request_ref == request_ref,
        )))
        assert sorted(attempt.attempt_number for attempt in attempts) == [1, 2, 3]


def test_direct_request_adoption_serializes_and_preserves_proof(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        utterance = _utterance("1542799000000000688", "Track the adoption fixture.")
        session.add(utterance)
        session.flush()
        staged = _item_stage(utterance, change_id="adopt-item", title="Adoption fixture",
                             include_scope=True)
        service = InteractiveAuthorityService(session)
        with patch.object(service.changesets, "_validate", return_value=[{
            "code": "fixture_implementation_validation", "category": "implementation_validation",
        }]):
            outcome = service.process_turn(
                utterance_ref=utterance.ref_id, request_key=utterance.request_key,
                actor_id=str(get_settings().operator_discord_user_id), intent_session_ref=None,
                expected_session_version=None, statements=[], relations=[],
                resolved_intent_json={"kind": "adoption_fixture"}, blocking_clarifications=[],
                content=ChangeSetContent(
                    basis_refs=[utterance.ref_id],
                    tracked_context_changes=[staged.patch.operations[0].action],
                ), changeset_ref=None, expected_changeset_version=None,
            )
        assert outcome["state"] == "blocked_validation"
        request_ref = outcome["semantic_request_ref"]
        request = session.scalar(select(SemanticRequest).where(
            SemanticRequest.ref_id == request_ref,
        ))
        assert request is not None
        binding = deepcopy(request.selected_option_binding)
        authority_hash = request.authority_scope_hash
        changeset = session.scalar(select(ChangeSet).where(
            ChangeSet.semantic_request_ref == request_ref,
        ))
        assert changeset is not None
        changeset_id = changeset.id
        original = session.scalar(select(ChangeSetRevision).where(
            ChangeSetRevision.change_set_id == changeset.id,
        ))
        assert original is not None
        original_id, original_hash = original.id, original.parameter_hash
        utterance_ref, request_key = utterance.ref_id, utterance.request_key
        original_item_count = session.scalar(select(func.count(Item.id)))
    payload = StageChangesInput(
        utterance_ref=utterance_ref, request_key=request_key,
        patch={"operations": [{"operation": "draft_adopt"}]},
    )
    digest = sha256_json(payload.model_dump(mode="json"))
    admitted: list[tuple[str, str]] = []
    for index in range(2):
        trace_ref = new_public_ref("trace")
        _review(
            factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_ref,
            call_id=f"adopt-review-{index}", ordinal=1, argument_hash="a" * 64,
        )
        token = _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id=f"adopt-{index}",
            ordinal=2, tool_name="docket_stage_changes", argument_hash=digest,
        )
        admitted.append((trace_ref, token))
    barrier = threading.Barrier(2)

    def adopt(values: tuple[str, str]) -> dict[str, Any]:
        barrier.wait(timeout=10)
        return _stage(factory, utterance_ref=utterance_ref, token=values[1],
                      argument_hash=digest, request=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adopt, values) for values in admitted]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(result["disposition"] for result in results) == [
        "draft_revision_conflict", "ready_to_commit",
    ]
    winner = next(index for index, row in enumerate(results)
                  if row["disposition"] == "ready_to_commit")
    assert results[winner]["observation_required"] is True
    trace_ref, token = admitted[winner]
    with factory.begin() as session:
        proof = session.get(RequestAssemblyAdoption, request_ref)
        assert proof is not None and proof.original_revision_id == original_id
        assert proof.proof_hash == sha256_json(proof.proof_json)
        assert session.scalar(select(func.count(Item.id))) == original_item_count
        request = session.scalar(select(SemanticRequest).where(
            SemanticRequest.ref_id == request_ref,
        ))
        assert request is not None and request.selected_option_binding == binding
        assert request.authority_scope_hash == authority_hash
        assert session.scalar(select(func.count(ChangeSetRevision.id)).where(
            ChangeSetRevision.change_set_id == changeset_id,
        )) == 2
    commit_token = _admit_committed(
        factory, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="adopt-stale-commit",
        ordinal=3, tool_name="docket_commit_changeset", argument_hash="b" * 64,
    )
    with factory.begin() as session:
        result = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=commit_token, assembly_argument_hash="b" * 64,
        )
        assert result["disposition"] == "draft_revision_conflict"
    _review(
        factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_ref,
        call_id="adopt-observe-new", ordinal=4, argument_hash="c" * 64,
    )
    commit_token = _admit_committed(
        factory, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="adopt-commit",
        ordinal=5, tool_name="docket_commit_changeset", argument_hash="d" * 64,
    )
    with factory.begin() as session:
        receipt = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=commit_token, assembly_argument_hash="d" * 64,
        )
        assert receipt["canonical_disposition"] == "committed"
    replay = _stage(factory, utterance_ref=utterance_ref, token=token,
                    argument_hash=digest, request=payload)
    assert replay == {**results[winner], "replayed": True}
    with factory() as session:
        assert session.scalar(select(func.count(Item.id))) == original_item_count + 1
        original = session.get(ChangeSetRevision, original_id)
        assert original is not None and original.parameter_hash == original_hash
    for sql in (
        "UPDATE request_assembly_adoptions SET proof_hash = :hash "
        "WHERE semantic_request_ref = :ref",
        "DELETE FROM request_assembly_adoptions WHERE semantic_request_ref = :ref",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(sql), {"ref": request_ref, "hash": "0" * 64})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed rewriting an immutable adoption proof")
    try:
        migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260911f5c4")
        assert migration is not None
        with (
            factory.kw["bind"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "Request adoption proofs exist" in str(exc)
    else:
        raise AssertionError("Downgrade discarded adoption evidence")
    with factory() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20260912b8f7"
        assert session.get(RequestAssemblyAdoption, request_ref) is not None


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
                    "selected_entry_ids": [f"postgres-schedule-{index:02d}" for index in range(30)],
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
        assert result["disposition"] == "saved_with_errors"
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
    with factory.begin() as session:
        binding = _bind_execution(session, _load_utterance(session, utterance_ref), label=trace_ref)
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "format": 2, "trace_ref": trace_ref, "call_id": "same-transport-call", "ordinal": 1,
        "utterance_ref": utterance_ref,
        **{key: binding[key] for key in (
            "execution_index", "execution_completion_token", "gateway_instance_ref",
        )},
        "tool_name": "docket_search_history", "argument_hash": sha256_json({}),
        "contract_version": CONTRACT_VERSION, "contract_hash": contract_hash("interactive"),
        "issued_at": now, "expires_at": now + 900,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).decode().rstrip("=")
    signature = hmac.new(
        get_settings().hermes_to_docket_token().encode(),
        b"docket-mcp-invocation-v2:" + encoded.encode(), hashlib.sha256,
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


def test_explicit_compiler_migration_requires_reobservation(factory: sessionmaker[Session]) -> None:
    utterance_ref, request_key = _create_utterance(
        factory, "1542799000000000680", "Track this PostgreSQL migration fixture.",
    )
    trace = new_public_ref("trace")

    def admit(name: str, ordinal: int) -> str:
        return _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace, call_id=f"migration-{ordinal}",
            ordinal=ordinal, tool_name=name, argument_hash="a" * 64,
        )

    token = admit("docket_stage_changes", 1)
    with factory.begin() as session:
        request = _item_stage(_load_utterance(session, utterance_ref), change_id="pinned",
                              title="Pinned compiler fixture", include_scope=True)
        initial = ChangeSetAssemblyService(session).stage(
            request, assembly_operation_token=token, assembly_argument_hash="a" * 64,
        )
        draft = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == initial["draft_ref"]))
        assert draft is not None
        old_revision = session.scalar(select(ChangeSetRevision).where(
            ChangeSetRevision.change_set_id == draft.id,
            ChangeSetRevision.revision == 1,
        ))
        assert old_revision is not None
        old_pin = old_revision.compiler_manifest_json
        old_id = old_revision.id
    migration_token = admit("docket_stage_changes", 2)
    with patch("docket.services.changeset_pins.EXECUTABLE_SCHEMA_VERSION", 2):
        with factory.begin() as session:
            migrated = ChangeSetAssemblyService(session).stage(
                StageChangesInput(utterance_ref=utterance_ref, request_key=request_key,
                                  patch={"operations": [{"operation": "draft_recompile"}]}),
                assembly_operation_token=migration_token, assembly_argument_hash="a" * 64,
            )
            assert migrated["observation_required"] is True
            assert migrated["current_revision"] == 2
        commit_token = admit("docket_commit_changeset", 3)
        with factory.begin() as session:
            blocked = ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=commit_token, assembly_argument_hash="a" * 64,
            )
            assert blocked["disposition"] == "draft_revision_conflict"
            old_revision = session.get(ChangeSetRevision, old_id)
            assert old_revision is not None and old_revision.compiler_manifest_json == old_pin
        reviewed = _review(
            factory, utterance_ref=utterance_ref, request_key=request_key,
            trace_ref=trace, call_id="migration-review", ordinal=4,
            argument_hash="a" * 64, view="diff",
        )
        assert any(row["subject_kind"] == "compiler_pin" for row in reviewed["items"])
        commit_token = admit("docket_commit_changeset", 5)
        with factory.begin() as session:
            receipt = ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=commit_token, assembly_argument_hash="a" * 64,
            )
            assert receipt["disposition"] == "committed"
        with factory.begin() as session:
            replay = ChangeSetAssemblyService(session).stage(
                StageChangesInput(utterance_ref=utterance_ref, request_key=request_key,
                                  patch={"operations": [{"operation": "draft_recompile"}]}),
                assembly_operation_token=migration_token, assembly_argument_hash="a" * 64,
            )
            assert replay["replayed"] is True
            assert replay["compiler_migration"] == migrated["compiler_migration"]
            assert session.scalar(select(func.count(AuditEvent.id)).where(
                AuditEvent.primary_ref == initial["draft_ref"],
                AuditEvent.event_type == "changeset.recompiled",
            )) == 1
    try:
        with factory.begin() as session:
            session.execute(text(
                "UPDATE change_set_revisions SET compiler_manifest_json = '{}' WHERE id = :id"
            ), {"id": old_id})
    except DBAPIError:
        pass
    else:
        raise AssertionError("Migration allowed mutation of the previous executable evidence")


def test_explicit_clear_patches_survive_postgresql_revision_and_recompile(
    factory: sessionmaker[Session],
) -> None:
    from docket.services.request_specifications import read_request_proposal

    utterance_ref, request_key = _create_utterance(
        factory, "1542799000000000742", "Clear the item description and reopen its task.",
    )
    trace_ref = new_public_ref("trace")
    with factory.begin() as session:
        item = Item(title="Keep title", description="Clear this", kind="smoke.request",
                    basis_refs=[utterance_ref], created_by_changeset_ref=new_public_ref("chg"))
        session.add(item)
        session.flush()
        task = Task(title="Follow up", item_ref=item.ref_id, task_state="completed",
                    completed_at=datetime(2026, 9, 10, tzinfo=UTC),
                    basis_refs=[utterance_ref], created_by_changeset_ref=new_public_ref("chg"))
        session.add(task)
        session.flush()
        item_ref, task_ref = item.ref_id, task.ref_id
    payloads = [{"description": None}, {"task_state": "in_progress", "completed_at": None}]
    request = StageChangesInput.model_validate({
        "utterance_ref": utterance_ref, "request_key": request_key,
        "expected_versions": {item_ref: 1, task_ref: 1},
        "assembly_scope": {"resolved_intent": {"intent": "clear description and reopen task"},
                           "allowed_mutation_types": ["item_modify", "task_modify"],
                           "target_refs": [item_ref, task_ref]},
        "patch": {"operations": [{"operation": "action_upsert", "action": {
            "mutation_type": f"{kind}_modify", "change_id": kind,
            "object_type": kind, "action": "update", "object_ref": ref,
            "payload": payload, "affected_fields": list(payload), "basis_refs": [utterance_ref],
        }} for kind, ref, payload in zip(["item", "task"], [item_ref, task_ref], payloads,
                                         strict=True)]},
    })

    def admit(ordinal: int, tool: str) -> str:
        return _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace_ref,
            call_id=f"patch-{ordinal}", ordinal=ordinal, tool_name=tool,
            argument_hash=str(ordinal) * 64,
        )

    token = admit(1, "docket_stage_changes")
    with factory.begin() as session:
        staged = ChangeSetAssemblyService(session).stage(
            request, assembly_operation_token=token, assembly_argument_hash="1" * 64,
        )
        assert staged["disposition"] == "ready_to_commit", staged
        changeset = session.scalar(select(ChangeSet).where(ChangeSet.ref_id == staged["draft_ref"]))
        assert changeset is not None
        request_ref = changeset.semantic_request_ref
        original_preview = deepcopy(changeset.compiler_manifest_json["canonical_patch_preview"])
        assert original_preview["effect_count"] == 2
        effects = {effect["change_id"]: effect for effect in original_preview["effects"]}
        assert effects["item"]["before"] == {"description": "Clear this"}
        assert effects["item"]["after"] == {"description": None}
        assert effects["task"]["before"]["task_state"] == "completed"
        assert effects["task"]["after"] == payloads[1]
    token = admit(2, "docket_stage_changes")
    with factory.begin() as session:
        migrated = ChangeSetAssemblyService(session).stage(
            StageChangesInput.model_validate({
                "utterance_ref": utterance_ref, "request_key": request_key,
                "patch": {"operations": [{"operation": "draft_recompile"}]},
            }), assembly_operation_token=token, assembly_argument_hash="2" * 64,
        )
        assert migrated["disposition"] == "ready_to_commit", migrated
        assert migrated["observation_required"] is True
        for version in (1, 2):
            proposal = read_request_proposal(session, semantic_request_ref=request_ref,
                                            version=version)
            assert [action.payload.model_dump(exclude_unset=True)
                    for action in proposal.direct_actions] == payloads
    token = admit(3, "docket_review_changeset")
    with factory.begin() as session:
        ChangeSetAssemblyService(session).review(
            ReviewChangesInput(utterance_ref=utterance_ref, request_key=request_key),
            assembly_operation_token=token, assembly_argument_hash="3" * 64,
        )
    token = admit(4, "docket_commit_changeset")
    with factory.begin() as session:
        receipt = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=token, assembly_argument_hash="4" * 64,
        )
        assert receipt["disposition"] == "committed", receipt
    with factory() as session:
        item = session.scalar(select(Item).where(Item.ref_id == item_ref))
        task = session.scalar(select(Task).where(Task.ref_id == task_ref))
        assert item is not None and item.description is None
        assert item.title == "Keep title" and item.kind == "smoke.request" and item.version == 2
        assert task is not None and task.completed_at is None
        assert task.title == "Follow up" and task.task_state == "in_progress" and task.version == 2
        revision = session.scalar(select(ChangeSetRevision).join(ChangeSet).where(
            ChangeSet.ref_id == staged["draft_ref"], ChangeSetRevision.revision == 1,
        ))
        assert revision is not None
        assert revision.compiler_manifest_json["canonical_patch_preview"] == original_preview
    # Paging the old revision after commit still reports its captured before-state.
    page = _review(
        factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_ref,
        call_id="patch-historical-diff", ordinal=5, argument_hash="5" * 64, view="diff", limit=100,
    )
    rows = [row for row in page["items"] if row["subject_kind"] == "canonical_target_effect"]
    assert any(row.get("before") == "Clear this" and row.get("after") is None for row in rows)
    assert any(row.get("before") == "completed" and row.get("after") == "in_progress"
               for row in rows)


def test_source_title_repair_survives_restart_and_replays_once(
    factory: sessionmaker[Session],
) -> None:
    import docket.services.changeset_assembly as assembly_module

    settings = get_settings()
    titles = ["2026 Fall Career Fair", "2026 Fall Career Fair", "2026 Business Career Fair"]
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for title in titles:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({title}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = BytesIO()
    writer.write(pdf)
    raw = pdf.getvalue()
    message_id = "1542799000000000681"
    request_key = f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
    with factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(OperatorUtteranceCapture(
            request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id, message_id=message_id,
            actor_id=settings.operator_discord_user_id, request_key=request_key,
            verbatim_text="Add the three source fairs to this fixture's Meetings lane.",
            attachments=[AttachmentManifest(
                transport_attachment_ref="1542799000000000682", filename="fairs.pdf",
                media_type="application/pdf", byte_size=len(raw), received_at=datetime.now(UTC),
                plaintext_base64=base64.b64encode(raw).decode(),
            )],
        ))
        utterance_ref = captured["ref"]
        source_ref = captured["attachments"][0]["ref"]
        account = ProviderAccount(provider="google", external_account_id="title-repair-smoke",
                                  capabilities=["google_calendar"], enabled=True)
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id, lane="title-repair-meetings", display_name="Meetings",
            color_hex="#3367D6", calendar_id="title-repair@example.com", status="active",
            basis_refs=[utterance_ref], created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane)
        session.flush()
        lane_ref = lane.ref_id
        fragments = AttachmentTextService(AttachmentEvidenceService(
            session, encryption_key=settings.attachment_encryption_key(),
            encryption_key_ref=settings.attachment_encryption_key_ref,
            max_attachment_bytes=settings.attachment_max_bytes,
            max_total_bytes=settings.attachment_total_max_bytes,
        )).read_pdf_text(source_ref=source_ref, cursor=None, max_text_bytes=8192, page_limit=3)

    trace_ref = new_public_ref("trace")

    def admit(tool: str, ordinal: int) -> str:
        return _admit_committed(
            factory, utterance_ref=utterance_ref, trace_ref=trace_ref,
            call_id=f"source-title-{ordinal}", ordinal=ordinal, tool_name=tool,
            argument_hash="a" * 64,
        )

    operations = [{"operation": "normalized_entry_upsert", "entry": {
        "entry_type": "scheduled_occurrence_entry", "import_entry_id": f"fair-{index}",
        "title": title, "lane_ref": lane_ref, "location": "Fixture Recreation Center",
        "timing": {"kind": "timed", "start_local": f"2026-09-{16 + index}T10:00:00",
                   "end_local": f"2026-09-{16 + index}T{14 if index == 2 else 15}:00:00",
                   "timezone": "America/Los_Angeles"},
        "evidence": {"source_ref": source_ref,
                     "source_fragment_locator": (
                         fragments["items"][index]["source_fragment_locator"]
                     ),
                     "source_fragment_hash": fragments["items"][index]["source_fragment_hash"],
                     "extractor_identifier": fragments["extractor_identifier"],
                     "extractor_version": fragments["extractor_version"]},
    }} for index, title in enumerate(titles)]
    compiler = assembly_module.compile_normalized_entry

    def faulty_compiler(*args: Any, **kwargs: Any) -> Any:
        compiled = compiler(*args, **kwargs)
        actions = deepcopy(list(compiled.actions))
        for action in actions:
            if action["mutation_type"] == "canonical_event_create":
                action["create_spec"]["title"] = "Incorrect duplicated title"
                action["create_spec"]["event_spec"]["title"] = "Incorrect duplicated title"
        return replace(compiled, actions=tuple(actions))

    token = admit("docket_stage_changes", 1)
    with (
        patch.object(assembly_module, "compile_normalized_entry", faulty_compiler),
        factory.begin() as session,
    ):
        staged = ChangeSetAssemblyService(session).stage(StageChangesInput.model_validate({
            "utterance_ref": utterance_ref, "request_key": request_key,
            "assembly_scope": {
                "resolved_intent": {"intent": "add the three source fairs"},
                "source_refs": [source_ref], "target_refs": [lane_ref],
                "normalized_entry_types": ["scheduled_occurrence_entry"],
                "selected_entry_ids": [f"fair-{index}" for index in range(3)],
            }, "patch": {"operations": operations},
        }), assembly_operation_token=token, assembly_argument_hash="a" * 64)
        assert staged["disposition"] == "saved_with_errors"
        draft_ref = staged["draft_ref"]

    migration_token = admit("docket_stage_changes", 2)
    migration = StageChangesInput(utterance_ref=utterance_ref, request_key=request_key,
                                 patch={"operations": [{"operation": "draft_recompile"}]})
    with factory.begin() as session:
        repaired = ChangeSetAssemblyService(session).stage(
            migration, assembly_operation_token=migration_token, assembly_argument_hash="a" * 64,
        )
        assert repaired["compiler_migration"]["source_title_repair_count"] == 3
        assert repaired["disposition"] == "ready_to_commit"
    _review(factory, utterance_ref=utterance_ref, request_key=request_key, trace_ref=trace_ref,
            call_id="title-repair-review", ordinal=3, argument_hash="a" * 64)
    token = admit("docket_commit_changeset", 4)
    with factory.begin() as session:
        committed = ChangeSetAssemblyService(session).commit(
            utterance_ref=utterance_ref, request_key=request_key,
            assembly_operation_token=token, assembly_argument_hash="a" * 64,
        )
        assert committed["disposition"] == "committed"
    with factory.begin() as session:
        replay = ChangeSetAssemblyService(session).stage(
            migration, assembly_operation_token=migration_token, assembly_argument_hash="a" * 64,
        )
        assert replay == {**repaired, "replayed": True}
        events = sorted(session.scalars(select(CanonicalEvent).where(
            CanonicalEvent.lane_ref == lane_ref,
        )), key=lambda event: event.event_spec["timing"]["start_local"])
        assert [event.title for event in events] == titles
        assert all(event.event_spec["title"] == event.title for event in events)
        assert [event.event_spec["timing"]["start_local"] for event in events] == [
            f"2026-09-{day}T10:00:00" for day in (16, 17, 18)
        ]
        assert session.scalar(select(func.count(Operation.id)).where(
            Operation.originating_changeset_ref == draft_ref,
        )) == 3
        assert session.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.primary_ref == draft_ref, AuditEvent.event_type == "changeset.recompiled",
        )) == 1
        versions = list(session.scalars(select(ChangeSetRevision).join(ChangeSet).where(
            ChangeSet.ref_id == draft_ref,
        ).order_by(ChangeSetRevision.revision)))
        assert len(versions) == 2
        assert versions[0].event_changes[0]["create_spec"]["title"] == "Incorrect duplicated title"
        assert versions[1].compiler_manifest_json["source_title_repair_proofs"]


def test_trace_history_survives_call_one_hundred_and_blocks_lossy_downgrade(
    factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    trace_ref = new_public_ref("trace")
    started = datetime.now(UTC)
    states: tuple[Literal["running", "completed"], ...] = ("running", "completed")
    with factory.begin() as session:
        source = _utterance("1542799000000000797", "Synthetic long trace.")
        session.add(source)
        session.flush()
        binding = _bind_execution(session, source, label=trace_ref, started_at=started)
    for ordinal in range(1, 104):
        with factory.begin() as session:
            service = McpTraceService(session)
            for state in states:
                service.update(trace_ref, McpTraceUpdate(
                    request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
                    source_channel_id=settings.chat_channel_id,
                    source_message_id="1542799000000000797",
                    actor_id=settings.operator_discord_user_id,
                    tool_contract_version=CONTRACT_VERSION,
                    tool_contract_hash=contract_hash("interactive"), caller_profile="interactive",
                    **_callback_binding(binding), updated_at=datetime.now(UTC),
                    call=McpTraceCallUpdate(
                        call_id=f"long-call-{ordinal}", ordinal=ordinal,
                        tool_name="docket_stage_changes", execution_boundary="local_rejection",
                        transport_state=state,
                        disposition="rejected_validation" if state == "completed" else None,
                    ),
                ))
    with factory() as session:
        trace = session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace_ref
        ))
        assert (
            trace is not None
            and _segment_for(session, trace_ref).last_ordinal
            == len(_segment_for(session, trace_ref).calls)
            == 103
        )
        trace_id = _segment_for(session, trace_ref).id
        page = HistoryService(session).get_entry(trace_ref, view="calls", limit=25)
        assert page["total_if_known"] == 103
        assert len(page["items"]) <= 25 and page["cursor"]
    try:
        # Test this specific guard even when a newer migration independently
        # refuses to discard its evidence. Keep the whole transaction rolled back.
        migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260912b8f7")
        assert migration is not None
        with (
            factory.kw["bind"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "downgrade would lose provenance" in str(exc)
    else:
        raise AssertionError("Downgrade should preserve the longer trace by refusing to proceed")
    with factory() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20260912b8f7"
        assert session.scalar(select(TraceExecutionSegment.last_ordinal).where(
            TraceExecutionSegment.id == trace_id
        )) == 103
    try:
        with factory.begin() as session:
            session.execute(text(
                "UPDATE trace_execution_segments SET last_ordinal = -1 WHERE id = :id"
            ), {"id": trace_id})
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL accepted a negative trace ordinal")


def test_trace_checkpoints_serialize_with_callbacks_and_rollback_pages(
    factory: sessionmaker[Session],
) -> None:
    utterance_ref, _request_key = _create_utterance(
        factory, "1542799000000000689", "Retain trace recovery evidence, not domain effects.",
    )
    settings = get_settings()
    trace_ref = new_public_ref("trace")
    started = datetime.now(UTC)
    with factory.begin() as session:
        binding = _bind_execution(session, _load_utterance(session, utterance_ref),
                                  label=trace_ref, started_at=started)
    context: dict[str, Any] = dict(
        request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id, source_message_id="1542799000000000689",
        actor_id=settings.operator_discord_user_id, caller_profile="interactive",
        tool_contract_version=CONTRACT_VERSION, tool_contract_hash=contract_hash("interactive"),
        **_callback_binding(binding), updated_at=started,
    )
    running = McpTraceCallUpdate(
        call_id="checkpoint-local", ordinal=1, tool_name="docket_stage_changes",
        execution_boundary="local_rejection", transport_state="running",
        received_argument_hash="a" * 64,
    )
    terminal = running.model_copy(update={
        "transport_state": "completed", "elapsed_ms": 7, "disposition": "rejected_validation",
    })
    checkpoint = McpTraceCheckpoint(
        **context, utterance_ref=utterance_ref, calls=[terminal],
    )
    barrier = threading.Barrier(2)

    def recover(is_checkpoint: bool) -> dict[str, Any]:
        with factory.begin() as session:
            barrier.wait(timeout=5)
            service = McpTraceService(session)
            if is_checkpoint:
                return service.checkpoint(trace_ref, checkpoint)
            return service.update(trace_ref, McpTraceUpdate(**context, call=running))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(recover, value) for value in (True, False)]
        for future in futures:
            assert future.result(timeout=10)["trace_ref"] == trace_ref
    with factory.begin() as session:
        service = McpTraceService(session)
        assert service.checkpoint(trace_ref, checkpoint)["disposition"] == "replayed_request"
        row = session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace_ref,
        ))
        assert (
            row is not None
            and _segment_for(session, trace_ref).calls[0]["transport_state"] == "completed"
        )
        version = row.version
        # One valid new observation followed by a gap must roll back the whole
        # page even if the caller handles the rejection inside its transaction.
        try:
            service.checkpoint(trace_ref, McpTraceCheckpoint(
                **context, utterance_ref=utterance_ref, calls=[
                    terminal.model_copy(update={"call_id": "second", "ordinal": 2}),
                    terminal.model_copy(update={"call_id": "fourth", "ordinal": 4}),
                ],
            ))
        except DocketError as exc:
            assert exc.code == "nonmonotonic_mcp_trace"
        else:
            raise AssertionError("Checkpoint accepted a missing observed ordinal")
        session.refresh(row)
        assert row.version == version and _segment_for(session, trace_ref).last_ordinal == 1
    for offset in (1, 26, 51):
        with factory.begin() as session:
            page = [terminal.model_copy(update={"call_id": f"recovered-{i}", "ordinal": i})
                    for i in range(offset + 1, min(offset + 26, 55))]
            McpTraceService(session).checkpoint(trace_ref, McpTraceCheckpoint(
                **context, utterance_ref=utterance_ref, calls=page,
                turn_status="completed" if offset == 51 else "running",
            ))
    with factory() as session:
        row = session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace_ref,
        ))
        assert (
            row is not None
            and row.status == "completed"
            and _segment_for(session, trace_ref).last_ordinal == 54
        )
        assert all(
            call["disposition"] == "rejected_validation"
            for call in _segment_for(session, trace_ref).calls
        )
        assert session.scalar(select(func.count(ToolInvocation.id)).where(
            ToolInvocation.trace_ref == trace_ref,
        )) == 0
        page = HistoryService(session).get_entry(trace_ref, view="calls", limit=25)
        assert page["counts"]["local_rejections"] == 54
        assert 0 < len(page["items"]) <= 25
        assert page["omitted_detail_count"] == 54 - len(page["items"])


def test_lost_admission_response_recovers_from_exact_local_trace(
    factory: sessionmaker[Session],
) -> None:
    utterance_ref, _request_key = _create_utterance(
        factory, "1542799000000000682", "Track the local-admission recovery fixture.",
    )
    trace_ref = new_public_ref("trace")
    first = _admit_committed(
        factory, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="lost-admission",
        ordinal=1, tool_name="docket_stage_changes", argument_hash="a" * 64,
    )
    settings = get_settings()
    with factory.begin() as session:
        binding = _bind_execution(session, _load_utterance(session, utterance_ref), label=trace_ref)
    states: tuple[Literal["running", "completed"], ...] = ("running", "completed")
    for state in states:
        with factory.begin() as session:
            McpTraceService(session).update(trace_ref, McpTraceUpdate(
                request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
                source_channel_id=settings.chat_channel_id,
                source_message_id="1542799000000000682",
                actor_id=settings.operator_discord_user_id,
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash("interactive"), caller_profile="interactive",
                **_callback_binding(binding), updated_at=datetime.now(UTC),
                call=McpTraceCallUpdate(
                    call_id="lost-admission", ordinal=1, tool_name="docket_stage_changes",
                    execution_boundary="local_rejection", transport_state=state,
                    disposition="failed" if state == "completed" else None,
                    received_argument_hash="a" * 64,
                ),
            ))
    second = _admit_committed(
        factory, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="corrected-next-call",
        ordinal=2, tool_name="docket_stage_changes", argument_hash="b" * 64,
    )
    with factory.begin() as session:
        result = ChangeSetAssemblyService(session).stage(
            _item_stage(_load_utterance(session, utterance_ref), change_id="local-recovery",
                        title="Local recovery fixture", include_scope=True),
            assembly_operation_token=second, assembly_argument_hash="b" * 64,
        )
        assert result["disposition"] == "ready_to_commit"
        prior = session.scalar(select(AssemblyOperation).where(
            AssemblyOperation.operation_key == first
        ))
        assert prior is not None and prior.state == "rejected"
        assert prior.result_json["error"]["code"] == "assembly_not_dispatched"
        assert session.scalar(select(func.count(ToolInvocation.id)).where(
            ToolInvocation.trace_ref == trace_ref
        )) == 0


def test_gateway_recovery_and_late_completion_serialize(factory: sessionmaker[Session]) -> None:
    for finalizer_waits in (False, True):
        message_id = f"154279900000000078{int(finalizer_waits)}"
        utterance_ref, request_key = _create_utterance(factory, message_id, "Track a smoke item.")
        trace_ref = new_public_ref("trace")
        with factory.begin() as session:
            gateway = GatewayLifetimeService(session).register(
                registration_key=uuid.uuid4(), instance_kind=f"recovery_smoke_{finalizer_waits}",
            )
            utterance = _load_utterance(session, utterance_ref)
            _bind_execution(session, utterance, label=trace_ref, gateway=gateway["ref"])
            stage_token = _admit(
                session, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="stage",
                ordinal=1, tool_name="docket_stage_changes", argument_hash="a" * 64,
            )
            ChangeSetAssemblyService(session).stage(
                _item_stage(utterance, change_id="one", title="Smoke item", include_scope=True),
                assembly_operation_token=stage_token, assembly_argument_hash="a" * 64,
            )
            commit_token = _admit(
                session, utterance_ref=utterance_ref, trace_ref=trace_ref, call_id="commit",
                ordinal=2, tool_name="docket_commit_changeset", argument_hash="b" * 64,
            )
            receipt = ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=commit_token, assembly_argument_hash="b" * 64,
            )
            segment = _segment_for(session, trace_ref)
            segment.calls = [{
                "call_id": "commit", "ordinal": 2, "tool_name": "docket_commit_changeset",
                "transport_state": "running", "received_argument_hash": "b" * 64,
            }]
            segment.last_ordinal = 2
            invocation = ToolInvocation(
                tool_name="docket_commit_changeset", caller_profile="interactive",
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash("interactive"),
                received_argument_hash="b" * 64, actor_ref=utterance.actor_ref,
                utterance_refs=[utterance_ref], trace_ref=trace_ref, trace_call_id="commit",
                trace_ordinal=2, gateway_instance_ref=gateway["ref"], trace_execution_id=segment.id,
            )
            session.add(invocation)
            session.flush()
            invocation_id = invocation.id
            # Clean shutdown is sufficient to trigger reconciliation and does
            # not require sleeping until a database lease clock expires.
            GatewayLifetimeService(session).clean_shutdown(str(gateway["ref"]))

        def finish(session: Session, invocation_id: uuid.UUID = invocation_id) -> None:
            ProvenanceFastMCP._finish_invocation(
                session, invocation_id, status="failed", normalized_argument_hash=None,
                result_refs=[], result_disposition="failed", error_code="internal_error",
            )

        started = threading.Event()

        def concurrent(
            started: threading.Event = started, finalizer_waits: bool = finalizer_waits,
        ) -> None:
            with factory.begin() as session:
                started.set()
                if finalizer_waits:
                    finish(session)
                else:
                    GatewayLifetimeService(session).expire_and_reconcile()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with factory.begin() as session:
                session.scalar(select(ToolInvocation).where(
                    ToolInvocation.id == invocation_id,
                ).with_for_update())
                pending = executor.submit(concurrent)
                assert started.wait(timeout=5)
                try:
                    pending.result(timeout=0.15)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Invocation finalization bypassed its row lock")
                if finalizer_waits:
                    GatewayLifetimeService(session).expire_and_reconcile()
                else:
                    finish(session)
            pending.result(timeout=10)
        with factory.begin() as session:
            GatewayLifetimeService(session).expire_and_reconcile()
            invocation = session.get(ToolInvocation, invocation_id)
            assert invocation is not None and invocation.result_disposition == "committed"
            assert invocation.domain_state == "succeeded" and invocation.error_code is None
            assert receipt["changeset_ref"] in invocation.result_refs
            trace = session.scalar(select(ConversationalToolTrace).where(
                ConversationalToolTrace.ref_id == trace_ref,
            ))
            assert trace is not None and trace.status == "interrupted"
            assert _segment_for(session, trace_ref).calls[0]["disposition"] == "committed"
            assert session.scalar(select(func.count(ChangeSet.id)).where(
                ChangeSet.semantic_request_ref == receipt["semantic_request_ref"],
                ChangeSet.state == "committed",
            )) == 1


def test_request_specifications_are_immutable_and_block_lossy_downgrade(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        row = session.scalar(select(SemanticRequestSpecification))
        assert row is not None
        key = {"ref": row.semantic_request_ref, "version": row.version}
        assert row.specification_hash == sha256_json(row.specification_json)
    for sql in (
        "UPDATE semantic_request_specifications SET specification_hash = :hash "
        "WHERE semantic_request_ref = :ref AND version = :version",
        "DELETE FROM semantic_request_specifications "
        "WHERE semantic_request_ref = :ref AND version = :version",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(sql), {**key, "hash": "0" * 64})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed rewriting immutable request evidence")
    try:
        migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260911e4b3")
        assert migration is not None
        with (
            factory.kw["bind"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "discard evidence" in str(exc)
    else:
        raise AssertionError("Downgrade discarded immutable request specifications")
    with factory() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20260912b8f7"
        assert session.get(SemanticRequestSpecification, (key["ref"], key["version"])) is not None


def test_initial_source_interpretations_are_immutable_across_connections(
    factory: sessionmaker[Session],
) -> None:
    from docket.services.request_interpretations import read_entry_interpretation

    with factory() as session:
        row = session.scalar(select(RequestEntryInterpretation))
        assert row is not None
        key = {"ref": row.semantic_request_ref, "entry": row.entry_id}
        expected_hash = row.interpretation_hash
        assert expected_hash == sha256_json(row.interpretation_json)
    for sql in (
        "UPDATE request_entry_interpretations SET interpretation_hash = :hash "
        "WHERE semantic_request_ref = :ref AND entry_id = :entry",
        "DELETE FROM request_entry_interpretations "
        "WHERE semantic_request_ref = :ref AND entry_id = :entry",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(sql), {**key, "hash": "0" * 64})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed rewriting initial source interpretation")
    try:
        migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260912a7e6")
        with (factory.kw["bind"].begin() as connection,
              Operations.context(MigrationContext.configure(connection))):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "Request interpretations exist" in str(exc)
    else:
        raise AssertionError("Downgrade discarded initial source interpretations")
    with factory() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20260912b8f7"
        interpreted = read_entry_interpretation(
            session, request_ref=key["ref"], entry_id=key["entry"],
        )
        assert sha256_json(interpreted.model_dump(mode="json")) == expected_hash
        assert interpreted.interpretation_state == "recorded_interpretation"


def test_timing_observations_serialize_and_preserve_evidence(
    factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    message = "1542799000000000690"
    start = datetime.now(UTC) - timedelta(seconds=3)
    ref = new_public_ref("trace")
    with factory.begin() as session:
        source = _utterance(message, "Synthetic timing-only fixture.")
        session.add(source)
        session.flush()
        utterance_ref = source.ref_id
        binding = _bind_execution(session, source, label=ref, started_at=start)
    span = TraceTimingInput(span_id=uuid.uuid4(), phase="model_request",
                           started_at=start, ended_at=start + timedelta(seconds=1))
    checkpoint = McpTraceCheckpoint(
        request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id, source_message_id=message,
        actor_id=settings.operator_discord_user_id, utterance_ref=utterance_ref,
        caller_profile="interactive", tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"),
        **_callback_binding(binding), updated_at=datetime.now(UTC), timings=[span],
    )
    barrier = threading.Barrier(2)

    def capture() -> str:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            return str(McpTraceService(session).checkpoint(ref, checkpoint)["disposition"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capture(), range(2)))
    assert sorted(results) == ["replayed_request", "updated"]
    with factory() as session:
        assert session.scalar(select(func.count(TraceTimingObservation.id)).where(
            TraceTimingObservation.trace_ref == ref,
        )) == 1
    for sql in (
        "UPDATE trace_timing_observations SET phase = 'context_schema' WHERE id = :id",
        "DELETE FROM trace_timing_observations WHERE id = :id",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(sql), {"id": span.span_id})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed rewriting a timing observation")
    migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260911a6d5")
    assert migration is not None
    try:
        with (factory.kw["bind"].begin() as connection,
              Operations.context(MigrationContext.configure(connection))):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "discard observations" in str(exc)
    else:
        raise AssertionError("A lossy timing downgrade was permitted")


def test_queue_boundary_survives_ingress_reclaim(factory: sessionmaker[Session]) -> None:
    from docket.services.continuity import ContinuityService
    from docket.services.trace_views import TraceViewService

    settings = get_settings()
    message, ref = "1542799000000000691", new_public_ref("trace")
    with factory.begin() as session:
        # The shared isolated database already has a live fixture gateway.
        lifetime = session.scalar(select(GatewayLifetime).where(
            GatewayLifetime.instance_kind == "hermes_discord_gateway",
            GatewayLifetime.status == "active",
        ))
        gateway = lifetime.ref_id if lifetime is not None else str(
            GatewayLifetimeService(session).register(
                registration_key=uuid.uuid4(), instance_kind="hermes_discord_gateway",
            )["ref"],
        )
        source = _utterance(message, "Synthetic queued timing fixture.")
        source.recorded_at = datetime.now(UTC) - timedelta(seconds=10)
        session.add(source)
        session.flush()
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key=f"timing-first:{ref}", lease_kind="interactive_turn",
            subject_ref=source.ref_id, gateway_instance_ref=str(gateway),
        )
        expected_queue = int((lease.claimed_at - source.recorded_at).total_seconds() * 1000)
        utterance_ref, token = source.ref_id, lease.completion_token
    start = datetime.now(UTC)
    with factory.begin() as session:
        binding = _bind_execution(session, _load_utterance(session, utterance_ref),
                                  label=ref, started_at=start, token=token)
        McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
            request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
            source_channel_id=settings.chat_channel_id, source_message_id=message,
            actor_id=settings.operator_discord_user_id, utterance_ref=utterance_ref,
            caller_profile="interactive",
            tool_contract_version=CONTRACT_VERSION, tool_contract_hash=contract_hash("interactive"),
            **_callback_binding(binding), updated_at=datetime.now(UTC), turn_status="completed",
        ))
        ContinuityService(session).complete_execution_lease(token)
        ContinuityService(session).acquire_execution_lease(
            lease_key=f"timing-recovery:{ref}", lease_kind="interactive_turn",
            subject_ref=utterance_ref, gateway_instance_ref=str(gateway),
        )
    with factory() as session:
        trace = session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == ref,
        ))
        assert trace is not None
        view = TraceViewService(session).snapshot(trace)
        assert view["timing"]["queue_ms"] == expected_queue
        assert view["timing"]["total_elapsed_ms"] >= expected_queue
        assert view["timing"]["model_ms"] is None
        assert view["timing_scope"].startswith("durable_receipt_to_trace_end")


def test_recovered_response_finalizes_original_turn_without_reexecution(
    factory: sessionmaker[Session],
) -> None:
    """Production failure sequence: old turn, replacement gateway, final callback."""
    settings = get_settings()
    message = "1542799000000000692"
    with factory.begin() as session:
        old = session.scalar(select(GatewayLifetime).where(
            GatewayLifetime.instance_kind == "hermes_discord_gateway",
            GatewayLifetime.status == "active",
        ))
        assert old is not None
        old_ref = old.ref_id
        request = OperatorUtteranceCapture(
            request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id, message_id=message,
            actor_id=settings.operator_discord_user_id,
            gateway_instance_ref=old_ref, verbatim_text="Synthetic response recovery.",
            request_key=f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message}:0",
        )
        captured = ProvenanceService(session).capture_operator_utterance(request)
        utterance_ref = captured["ref"]
        original = TraceExecutionService(session).bind(
            utterance_ref=utterance_ref,
            execution_completion_token=captured["deferred_ingress"]["execution_completion_token"],
            gateway_instance_ref=old_ref, tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"), turn_started_at=datetime.now(UTC),
        )
        intents = IntentSessionService(session)
        intent, _ = intents.open(IntentSessionOpen(source_utterance_ref=utterance_ref))
        intents.append_turn(IntentTurnAppend(
            intent_session_ref=intent.ref_id, utterance_ref=utterance_ref,
            gateway_instance_ref=old_ref,
        ))
        old.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
        operation_count = session.scalar(select(func.count(Operation.id)))
    with factory.begin() as session:
        new_ref = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(), instance_kind="hermes_discord_gateway"
        )["ref"]
        resumed_request = request.model_copy(update={"gateway_instance_ref": new_ref})
        resumed = ProvenanceService(session).capture_operator_utterance(resumed_request)
        token = resumed["deferred_ingress"]["execution_completion_token"]
        binding = TraceExecutionService(session).bind(
            utterance_ref=utterance_ref, execution_completion_token=token,
            gateway_instance_ref=new_ref, tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"), turn_started_at=datetime.now(UTC),
        )
        assert binding["trace_ref"] == original["trace_ref"]
        assert binding["execution_index"] == 2
        response_request = GatewayAgentResponseCapture(
            request_id=uuid.uuid4(), guild_id=request.guild_id, channel_id=request.channel_id,
            source_message_id=message, actor_id=request.actor_id, utterance_ref=utterance_ref,
            turn_id="recovered", session_id="response-smoke", model_identifier="smoke",
            verbatim_text="Recovered synthetic result.", generated_at=datetime.now(UTC),
            trace_ref=binding["trace_ref"], execution_index=binding["execution_index"],
            execution_completion_token=token, gateway_instance_ref=new_ref,
        )
        response = ProvenanceService(session).capture_agent_response(response_request)
    # The response arrives durably before its acknowledgement; a failed delivery
    # and a repeated capture cannot create another response or model admission.
    with factory.begin() as session:
        result = ContinuityService(session).complete_interactive_ingress(
            completion_token=token, ingress_ref=resumed["deferred_ingress"]["ref"],
            gateway_instance_ref=new_ref, outcome="failed", error_code="discord_delivery_failed",
        )
        assert result["disposition"] == "completed"
        replay = ProvenanceService(session).capture_agent_response(response_request)
        assert replay["ref"] == response["ref"] and replay["disposition"] == "replayed_request"
        duplicate = ProvenanceService(session).capture_operator_utterance(resumed_request)
        assert duplicate["deferred_ingress"]["state"] == "completed"
        assert duplicate["deferred_ingress"]["execution_completion_token"] is None
        turn = session.scalar(select(IntentTurn).where(IntentTurn.utterance_ref == utterance_ref))
        assert turn is not None and turn.gateway_instance_ref == old_ref
        assert turn.response_disposition == "final_response"
        assert session.scalar(select(func.count(AgentResponse.id)).where(
            AgentResponse.responds_to_utterance_refs[0].as_string() == utterance_ref
        )) == 1
        assert session.scalar(select(func.count(Operation.id))) == operation_count


def main() -> None:
    database_url = os.environ["DOCKET_DATABASE_URL"]
    engine = configure_database(database_url)
    assert engine.dialect.name == "postgresql"
    factory = sessionmaker(engine, expire_on_commit=False)
    checks = (
        test_retained_trace_migration_round_trip,
        test_cold_restart_reuses_one_trace_and_staged_request,
        test_same_attempt_concurrent_calls_bind_old_revision,
        test_cross_attempt_stale_edit_and_commit_are_rejected,
        test_one_changeset_lineage_per_semantic_request,
        test_pre_admitted_initial_stages_share_one_request,
        test_direct_receipt_resume_admissions_serialize,
        test_direct_request_adoption_serializes_and_preserves_proof,
        test_thirty_entry_schedule_commits_once,
        test_native_and_deferred_ingress_claim_once,
        test_relative_date_capture_serializes,
        test_occurrence_commits_serialize_and_identity_is_immutable,
        test_diff_pages_keep_both_revisions_across_connections,
        test_invocation_binding_transport_retries_serialize,
        test_explicit_compiler_migration_requires_reobservation,
        test_explicit_clear_patches_survive_postgresql_revision_and_recompile,
        test_source_title_repair_survives_restart_and_replays_once,
        test_trace_history_survives_call_one_hundred_and_blocks_lossy_downgrade,
        test_trace_checkpoints_serialize_with_callbacks_and_rollback_pages,
        test_lost_admission_response_recovers_from_exact_local_trace,
        test_gateway_recovery_and_late_completion_serialize,
        test_request_specifications_are_immutable_and_block_lossy_downgrade,
        test_initial_source_interpretations_are_immutable_across_connections,
        test_timing_observations_serialize_and_preserve_evidence,
        test_queue_boundary_survives_ingress_reclaim,
        test_recovered_response_finalizes_original_turn_without_reexecution,
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

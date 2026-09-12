"""Exact everyday outcomes through the authenticated, staged-only MCP boundary."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from trace_support import bind_execution

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.public_refs import new_public_ref
from docket.mcp.server import mcp
from docket.models import (
    AssemblyOperation,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Entity,
    Item,
    LaneRoutingDecision,
    Operation,
    OperatorUtterance,
    ProviderAccount,
    SemanticRequest,
    Task,
    TemporalBinding,
    ToolInvocation,
)
from docket.services.changeset_assembly import ChangeSetAssemblyAdmissionService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _utterance(text):
    settings = get_settings()
    message_id = "1542799000000000998"
    return OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}",
        said_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0",
    )


def _invoke(session_factory, utterance, name, arguments, ordinal):
    """Supply execution bookkeeping as the gateway does, never as model arguments."""
    settings = get_settings()
    digest = sha256_json(arguments)
    with session_factory.begin() as session:
        binding = bind_execution(session, utterance, label="ordinary-workflow")
        execution = {
            key: binding[key]
            for key in (
                "execution_index",
                "execution_completion_token",
                "gateway_instance_ref",
            )
        }
        admission = ChangeSetAssemblyAdmissionService(session).admit(
            utterance_ref=utterance.ref_id,
            trace_ref=binding["trace_ref"],
            **execution,
            upstream_tool_call_id=f"ordinary-{ordinal}",
            trace_ordinal=ordinal,
            tool_name=name,
            argument_hash=digest,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            source_message_id=utterance.request_key.split(":")[3],
            actor_id=settings.operator_discord_user_id,
        )
    now = int(datetime.now(UTC).timestamp())
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "format": 2,
                    "trace_ref": binding["trace_ref"],
                    "call_id": f"ordinary-{ordinal}",
                    "ordinal": ordinal,
                    "utterance_ref": utterance.ref_id,
                    **execution,
                    "tool_name": name,
                    "argument_hash": digest,
                    "contract_version": CONTRACT_VERSION,
                    "contract_hash": contract_hash("interactive"),
                    "issued_at": now,
                    "expires_at": now + 900,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        settings.hermes_to_docket_token().encode(),
        b"docket-mcp-invocation-v2:" + encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    result = asyncio.run(
        mcp.call_tool(
            name,
            {
                **arguments,
                "utterance_ref": utterance.ref_id,
                "request_key": utterance.request_key,
                "assembly_operation_token": admission["assembly_operation_token"],
                "assembly_argument_hash": digest,
                "invocation_binding": f"{encoded}.{signature}",
            },
        )
    )
    assert isinstance(result, tuple)
    assert len(json.dumps(result[1]).encode()) <= 16 * 1024
    return result[1]


def _stage_commit(session_factory, utterance, actions, target_refs):
    stage = _invoke(
        session_factory,
        utterance,
        "docket_stage_changes",
        {
            "assembly_scope": {
                "resolved_intent": {"request": utterance.verbatim_text},
                "allowed_mutation_types": [action["mutation_type"] for action in actions],
                "planned_create_types": [action["object_type"] for action in actions],
                "target_refs": target_refs,
            },
            "patch": {
                "operations": [
                    {"operation": "action_upsert", "action": action} for action in actions
                ]
            },
        },
        1,
    )
    assert stage["disposition"] == "ready_to_commit", stage
    assert stage["assembly_ready"] is True
    with session_factory() as session:
        for model in (Item, Task, TemporalBinding, CanonicalEvent, Operation):
            assert session.scalar(select(func.count(model.id))) == 0
    # No review and no model-supplied IDs, revisions, hashes, or direct payload.
    receipt = _invoke(session_factory, utterance, "docket_commit_changeset", {}, 2)
    assert receipt["disposition"] == "committed", receipt
    with session_factory() as session:
        calls = list(session.scalars(select(ToolInvocation).order_by(ToolInvocation.trace_ordinal)))
        assert [call.tool_name for call in calls] == [
            "docket_stage_changes",
            "docket_commit_changeset",
        ]
        assert all(call.transport_state == "completed" for call in calls)
        assert all(call.domain_state == "succeeded" for call in calls)
        assert calls[1].result_disposition == "committed"
        assert calls[1].normalized_argument_hash == sha256_json({})
        assert session.scalar(select(func.count(AssemblyOperation.id))) == 2
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 1
        assert session.scalar(select(func.count(SemanticRequest.id))) == 1
        assert (
            session.scalar(select(SemanticRequest.authority_availability)) == "consumed_committed"
        )
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert session.scalar(select(ChangeSet.state)) == "committed"
    return receipt


def _create(utterance, variant, change_id, object_type, fields, spec):
    return {
        "mutation_type": variant,
        "change_id": change_id,
        "action": "create",
        "object_type": object_type,
        "affected_fields": fields,
        "basis_refs": [utterance.ref_id],
        "create_spec": spec,
    }


@pytest.mark.integration
def test_simple_event_stage_commit_without_review(session_factory):
    utterance = _utterance(
        "Add a PolyUAS meeting September 16, 2026, 10-11 AM America/Los_Angeles, "
        "Engineering IV Room 121, to my Meetings calendar."
    )
    with session_factory.begin() as session:
        session.add(utterance)
        account = ProviderAccount(
            provider="google",
            external_account_id="ordinary-workflow-fixture",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="meetings",
            display_name="Meetings",
            calendar_id="meetings@example.com",
            color_hex="#3367D6",
            status="active",
            basis_refs=[utterance.ref_id],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane)
        session.flush()
    timing = {
        "kind": "timed",
        "start_local": "2026-09-16T10:00:00",
        "end_local": "2026-09-16T11:00:00",
        "timezone": "America/Los_Angeles",
    }
    receipt = _stage_commit(
        session_factory,
        utterance,
        [
            _create(
                utterance,
                "lane_routing_decision_create",
                "route",
                "lane_routing_decision",
                ["lane"],
                {
                    "lane_ref": lane.ref_id,
                    "event_change_id": "meeting",
                    "decision_kind": "explicit_operator",
                    "operator_confirmed": True,
                },
            ),
            _create(
                utterance,
                "canonical_event_create",
                "meeting",
                "canonical_event",
                ["event", "lane"],
                {
                    "title": "PolyUAS meeting",
                    "lane_ref": lane.ref_id,
                    "event_spec": {
                        "title": "PolyUAS meeting",
                        "calendar_lane": "meetings",
                        "location": "Engineering IV Room 121",
                        "timing": timing,
                    },
                },
            ),
        ],
        [lane.ref_id],
    )
    assert receipt["canonical_disposition"] == "committed"
    assert receipt["provider_disposition"] == "queued"
    assert receipt["provider_operation_count"] == 1
    with session_factory() as session:
        event = session.scalars(select(CanonicalEvent)).one()
        assert event.title == event.event_spec["title"] == "PolyUAS meeting"
        assert {k: v for k, v in event.event_spec["timing"].items() if v is not None} == timing
        assert event.event_spec["location"] == "Engineering IV Room 121"
        assert event.lane_ref == lane.ref_id
        assert event.event_spec["calendar_lane"] == "meetings"
        assert not event.event_spec.get("recurrence")
        assert session.scalar(select(func.count(LaneRoutingDecision.id))) == 1
        operation = session.scalars(select(Operation)).one()
        assert operation.status == "pending"
        assert operation.canonical_target_refs == [event.ref_id]
        assert event.basis_refs == [utterance.ref_id]
        assert event.created_by_changeset_ref == receipt["changeset_ref"]
        # No invented work or tracked-item obligation around a simple meeting.
        assert session.scalar(select(func.count(Item.id))) == 0
        assert session.scalar(select(func.count(Task.id))) == 0


@pytest.mark.integration
def test_homework_stage_commit_without_review(session_factory):
    utterance = _utterance("I have HW2 due for MATH 1151 at 9 AM on September 8, 2026.")
    with session_factory.begin() as session:
        session.add(utterance)
        course = Entity(
            entity_kind="course_section",
            display_name="MATH 1151 F26",
            normalized_name="math 1151 f26",
            basis_refs=[],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(course)
        session.flush()
    due = {
        "kind": "datetime",
        "local_datetime": "2026-09-08T09:00:00",
        "timezone": "America/Los_Angeles",
    }
    receipt = _stage_commit(
        session_factory,
        utterance,
        [
            _create(
                utterance,
                "item_create",
                "hw2",
                "item",
                ["title", "kind"],
                {
                    "title": "MATH 1151 HW2",
                    "kind": "academic.assignment",
                    "context_entity_refs": [course.ref_id],
                },
            ),
            _create(
                utterance,
                "task_create",
                "complete-hw2",
                "task",
                ["item_ref", "task_state"],
                {
                    "item_change_id": "hw2",
                    "title": "Complete MATH 1151 HW2",
                },
            ),
            _create(
                utterance,
                "temporal_binding_create",
                "hw2-due",
                "temporal_binding",
                ["subject_ref", "role", "temporal_value"],
                {
                    "subject_change_id": "complete-hw2",
                    "role": "due_by",
                    "temporal_value": due,
                },
            ),
        ],
        [course.ref_id],
    )
    assert receipt["provider_operation_count"] == 0
    with session_factory() as session:
        item = session.scalars(select(Item)).one()
        task = session.scalars(select(Task)).one()
        temporal = session.scalars(select(TemporalBinding)).one()
        assert item.title == "MATH 1151 HW2"
        assert item.kind == "academic.assignment"
        assert item.context_entity_refs == [course.ref_id]
        assert task.title == "Complete MATH 1151 HW2"
        assert task.item_ref == item.ref_id
        assert task.task_state == "not_started"
        assert temporal.subject_ref == task.ref_id
        assert temporal.role == "due_by"
        assert temporal.temporal_value == {**due, "fold": None}
        for row in (item, task, temporal):
            assert row.basis_refs == [utterance.ref_id]
            assert row.created_by_changeset_ref == receipt["changeset_ref"]
        assert session.scalar(select(func.count(Entity.id))) == 1
        assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
        assert session.scalar(select(func.count(Operation.id))) == 0

import time
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from sqlalchemy import select

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.canonical import sha256_json
from docket.domain.enums import CommandStatus, IntentAuthority, RecordStatus
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.models import CommandRequest, SourceItem
from docket.models.base import utc_now
from docket.providers.google.gmail_runtime import get_gmail_read_provider
from docket.providers.google.runtime import get_calendar_read_provider
from docket.schemas.actions import (
    CalendarEventProposal,
    CourseReconciliationMode,
    DirectExecutionResult,
    EventEntityBindingInput,
    ProposalResult,
    ProposeCalendarEventInput,
    ProposeCourseReconciliationInput,
)
from docket.schemas.authority import (
    CanonicalChangeInput,
    ChangeSetContent,
    ConflictRef,
    ConflictResolve,
    SemanticOptionDraft,
    SessionRef,
    StatementInput,
    StatementRef,
    StatementRelationInput,
    UtteranceRef,
)
from docket.schemas.calendar import (
    CalendarEventResultView,
    CalendarFreshness,
    CalendarLane,
    CalendarLaneEventSelection,
    CalendarLookupInput,
    CalendarProfileInput,
    CalendarRelativeDay,
    CalendarReminderPlanInput,
    ConfigureCalendarLaneInput,
    DeleteCalendarLaneInput,
    MigrateCalendarLaneEventsInput,
    SetCalendarProfileInput,
)
from docket.schemas.entities import (
    EntityAttributeKey,
    EntityAttributes,
    EntityName,
    EntityPredicate,
    EntityRelationAttributes,
    EntityRelationDirection,
    EntitySearchStatus,
)
from docket.schemas.intelligence import SourceRef
from docket.schemas.queue import (
    IgnoreQueueItemInput,
    QueuePriority,
    QueueStatus,
    SnoozeQueueItemInput,
)
from docket.schemas.records import (
    ArchiveRecordInput,
    CourseData,
    CourseIdentity,
    DiscordId,
    DiscordRequestKey,
    GenericIdentity,
    GenericRecordData,
    RecordSourceInput,
    RecordType,
    RestoreRecordInput,
    StoreRecordInput,
    TermData,
    TermIdentity,
    UpdateRecordInput,
    validate_discord_request_fields,
)
from docket.schemas.registry import EntityRef
from docket.schemas.triage import EntityClass
from docket.services.accounts import AccountService
from docket.services.action_read import ActionReadService
from docket.services.calendar_actions import CalendarActionService
from docket.services.calendar_lanes import CalendarLaneService
from docket.services.calendar_profile import CalendarProfileService
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService
from docket.services.conflicts import ConflictService
from docket.services.course_reconciliation import CourseReconciliationService
from docket.services.entities import EntityService
from docket.services.history import HistoryService
from docket.services.intelligence import IntelligenceService
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.network import NetworkQueryService
from docket.services.queue import QueueService
from docket.services.records import RecordService, serialize_record
from docket.services.reminders import ReminderRuleService
from docket.services.source_context import validate_configured_discord_source

mcp = ProvenanceFastMCP(
    "docket",
    caller_profile="interactive",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["docket:8000", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    ),
)

CalendarId = Annotated[str, Field(min_length=1, max_length=1024)]
CalendarLimit = Annotated[int, Field(ge=1, le=100)]
CalendarTextFilter = Annotated[str, Field(max_length=200)]
ActionWaitSeconds = Annotated[float, Field(ge=0, le=30)]
EntitySearchQuery = Annotated[str, Field(min_length=1, max_length=256)]
EntitySearchLimit = Annotated[int, Field(ge=1, le=50)]
EntityRetractionReason = Annotated[str, Field(min_length=1, max_length=1000)]
HistoryView = Literal["summary", "audit"]


def _offset_cursor(cursor: str | None) -> int:
    try:
        offset = int(cursor or "0")
    except ValueError as exc:
        raise DocketError(
            code="invalid_cursor", message="Cursor must be a non-negative offset."
        ) from exc
    if offset < 0:
        raise DocketError(code="invalid_cursor", message="Cursor must be a non-negative offset.")
    return offset


NetworkLimit = Annotated[int, Field(ge=1, le=100)]
NetworkDepth = Annotated[int, Field(ge=1, le=3)]
NetworkNodeLimit = Annotated[int, Field(ge=1, le=100)]


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DocketError):
        return exc.as_dict()
    return {
        "ok": False,
        "error": {
            "code": "internal_error",
            "message": "Docket encountered an internal processing failure.",
            "details": {},
        },
    }


def _model_proposal_result(result: ProposalResult) -> dict[str, Any]:
    """Return the proposal result exposed to the model-facing MCP client.

    The short code remains in Docket's durable action/outbox state for an
    operator-only break-glass path. It is deliberately absent from the model
    response so ordinary agent guidance cannot regress from persistent Discord
    controls to legacy typed approval messages.
    """
    payload = result.model_dump(mode="json", exclude={"short_code"})
    payload["approval_surface"] = {
        "kind": "discord_button_card",
        "location": "today's ISO-dated thread under the configured Docket queue",
        "delivery_status": result.projection_status,
        "operator_instruction": ("Use the Approve or Reject button on the projected Docket card."),
        "typed_code_policy": "break_glass_only_not_for_agent_guidance",
    }
    return payload


def _model_calendar_intent_result(
    result: ProposalResult | DirectExecutionResult,
) -> dict[str, Any]:
    if isinstance(result, ProposalResult):
        return _model_proposal_result(result)
    payload = result.model_dump(mode="json")
    payload["execution_surface"] = {
        "kind": "direct_operation",
        "status": result.operation_status,
        "operator_instruction": (
            "The explicit command is durably queued. Do not ask for approval or claim "
            "provider completion until Docket reports it."
        ),
    }
    return payload


def _model_course_reconciliation_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    payload.pop("short_code", None)
    if payload.get("approval_id") is not None:
        payload["approval_surface"] = {
            "kind": "discord_button_card",
            "location": "today's ISO-dated thread under the configured Docket queue",
            "delivery_status": payload.get("projection_status", "pending"),
            "operator_instruction": (
                "Review the per-meeting delta, then use the card's Approve or Reject button."
            ),
            "typed_code_policy": "break_glass_only_not_for_agent_guidance",
        }
    return payload


def _validate_entity_write(
    session: Any,
    *,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> None:
    validate_discord_request_fields(request_key, source, actor_id)
    validate_configured_discord_source(session, source, actor_id)


def _execute_entity_write(
    session: Any,
    *,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    operation_name: str,
    payload: dict[str, Any],
    execute: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Consume one trusted entity command idempotently in the caller transaction."""
    _validate_entity_write(session, request_key=request_key, source=source, actor_id=actor_id)
    command_payload = {
        **payload,
        "source": source.model_dump(mode="json"),
        "actor_id": actor_id,
    }
    input_sha256 = sha256_json(command_payload)
    existing = session.scalar(
        select(CommandRequest).where(CommandRequest.request_key == request_key)
    )
    if existing is not None:
        if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
            raise IdempotencyConflict(
                request_key,
                existing_operation=existing.operation_name,
                attempted_operation=operation_name,
            )
        if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
            return {**existing.result, "disposition": "replayed_request"}
        raise DocketError(
            code="request_in_progress",
            message="The entity request exists but has not completed successfully.",
            details={"request_key": request_key, "status": existing.status},
        )
    command = CommandRequest(
        request_key=request_key,
        operation_name=operation_name,
        input_sha256=input_sha256,
        actor_type="hermes",
        actor_id=actor_id,
        status=CommandStatus.IN_PROGRESS.value,
    )
    session.add(command)
    session.flush()
    result = execute()
    command.status = CommandStatus.SUCCEEDED.value
    command.result = result
    command.completed_at = utc_now()
    return result


def docket_resolve_entity(
    entity_class: EntityClass,
    mention: EntityName,
) -> dict[str, Any]:
    """Resolve an entity mention against Docket's emergent canonical registry.

    Returns resolved, unresolved, ambiguous, or provisional explicitly. It never
    creates a seed list or silently picks among multiple matching identities.
    """
    try:
        with session_scope() as session:
            service = EntityService(session)
            result = service.resolve(
                entity_class=entity_class,
                mention=mention,
            )
            relationships = (
                service.relationships(result.resolved_entity.entity_id)
                if result.resolved_entity is not None
                else []
            )
            return {
                "ok": True,
                "resolution": result.model_dump(mode="json"),
                "aliases": (
                    service.snapshot(result.resolved_entity.entity_id).model_dump(mode="json")[
                        "aliases"
                    ]
                    if result.resolved_entity is not None
                    else []
                ),
                "relationships": relationships,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_create_entity(
    entity_class: EntityClass,
    canonical_name: EntityName,
    attributes: EntityAttributes,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Create or confirm one genuinely new canonical entity from current user intent."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_create_entity",
                payload={
                    "entity_class": entity_class,
                    "canonical_name": canonical_name,
                    "attributes": attributes.model_dump(mode="json", exclude_none=True),
                },
                execute=lambda: {
                    "ok": True,
                    "entity": service.create(
                        entity_class=entity_class,
                        canonical_name=canonical_name,
                        attributes=attributes,
                        authority=IntentAuthority.EXPLICIT_USER,
                        actor_type="hermes",
                        actor_id=actor_id,
                    ).model_dump(mode="json"),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_update_entity(
    entity_id: uuid.UUID,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    canonical_name: EntityName | None = None,
    attribute_updates: EntityAttributes | None = None,
    remove_attribute_keys: list[EntityAttributeKey] | None = None,
) -> dict[str, Any]:
    """Patch one exact entity with optimistic versioning.

    ``attribute_updates`` changes only the supplied keys and preserves all other
    metadata. Use ``remove_attribute_keys`` for deliberate deletion; never send a
    reconstructed whole profile merely to change one fact.
    """
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_update_entity",
                payload={
                    "entity_id": str(entity_id),
                    "expected_version": expected_version,
                    "canonical_name": canonical_name,
                    "attribute_updates": (
                        attribute_updates.model_dump(mode="json", exclude_none=True)
                        if attribute_updates is not None
                        else None
                    ),
                    "remove_attribute_keys": remove_attribute_keys,
                },
                execute=lambda: {
                    "ok": True,
                    "entity": service.update(
                        entity_id=entity_id,
                        expected_version=expected_version,
                        canonical_name=canonical_name,
                        attribute_updates=attribute_updates,
                        remove_attribute_keys=remove_attribute_keys,
                        authority=IntentAuthority.EXPLICIT_USER,
                        actor_id=actor_id,
                    ).model_dump(mode="json"),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_add_entity_alias(
    entity_id: uuid.UUID,
    alias: EntityName,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Teach Docket an explicit alias for one exact canonical entity."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_add_entity_alias",
                payload={"entity_id": str(entity_id), "alias": alias},
                execute=lambda: {
                    "ok": True,
                    "entity": service.add_alias(
                        entity_id=entity_id,
                        alias=alias,
                        authority=IntentAuthority.EXPLICIT_USER,
                    ).model_dump(mode="json"),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_relate_entities(
    subject_entity_id: uuid.UUID,
    predicate: EntityPredicate,
    object_entity_id: uuid.UUID,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    attributes: EntityRelationAttributes | None = None,
) -> dict[str, Any]:
    """Persist one directional relationship between two exact canonical entities.

    Read the predicate as ``subject predicate object``: for example, the operator
    person ``works_for`` an organization, while another person ``advises`` the
    operator. Existing metadata is never silently overwritten.
    """
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_relate_entities",
                payload={
                    "subject_entity_id": str(subject_entity_id),
                    "predicate": predicate,
                    "object_entity_id": str(object_entity_id),
                    "attributes": (
                        attributes.model_dump(mode="json", exclude_none=True)
                        if attributes is not None
                        else None
                    ),
                },
                execute=lambda: {
                    "ok": True,
                    "relation_id": str(
                        service.relate(
                            subject_entity_id=subject_entity_id,
                            predicate=predicate,
                            object_entity_id=object_entity_id,
                            authority=IntentAuthority.EXPLICIT_USER,
                            attributes=attributes,
                            actor_id=actor_id,
                        )
                    ),
                },
            )
    except Exception as exc:
        return _error(exc)


def docket_get_entity(entity_id: uuid.UUID) -> dict[str, Any]:
    """Read one exact entity with its aliases and active directional relationships.

    Use this immediately before relying on stored metadata or changing an entity or
    relationship. It is the authoritative entity snapshot, not conversational memory.
    """
    try:
        with session_scope() as session:
            snapshot = EntityService(session).snapshot(entity_id)
            return {"ok": True, **snapshot.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


def docket_search_entities(
    query: EntitySearchQuery | None = None,
    entity_class: EntityClass | None = None,
    status: EntitySearchStatus = "active_or_provisional",
    predicate: EntityPredicate | None = None,
    related_entity_id: uuid.UUID | None = None,
    direction: EntityRelationDirection = "any",
    is_operator: bool | None = None,
    limit: EntitySearchLimit = 20,
) -> dict[str, Any]:
    """Search Docket's canonical registry before asking the operator for known facts.

    Search covers canonical names, aliases, and validated metadata. Relationship filters
    let Hermes infer bounded facts already present in Docket: for example, find people
    who ``advise`` the operator or organizations the operator ``member_of``. Direction
    describes the returned entity's side of ``subject predicate object``. Results are
    bounded and include aliases plus active relationships. Do not treat no result as
    permission to invent a fact.
    """
    try:
        with session_scope() as session:
            results = EntityService(session).search(
                query=query,
                entity_class=entity_class,
                status=status,
                predicate=predicate,
                related_entity_id=related_entity_id,
                direction=direction,
                is_operator=is_operator,
                limit=limit,
            )
            return {
                "ok": True,
                "count": len(results),
                "entities": [result.model_dump(mode="json") for result in results],
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_update_entity_relation(
    relation_id: uuid.UUID,
    expected_version: int,
    attributes: EntityRelationAttributes,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Replace the metadata of one exact active relationship with version checking."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_update_entity_relation",
                payload={
                    "relation_id": str(relation_id),
                    "expected_version": expected_version,
                    "attributes": attributes.model_dump(mode="json", exclude_none=True),
                },
                execute=lambda: {
                    "ok": True,
                    "relationship": service.update_relation(
                        relation_id=relation_id,
                        expected_version=expected_version,
                        attributes=attributes,
                        authority=IntentAuthority.EXPLICIT_USER,
                        actor_id=actor_id,
                    ),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_retract_entity_relation(
    relation_id: uuid.UUID,
    expected_version: int,
    reason: EntityRetractionReason,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Retract one wrong or ended relationship without erasing its history."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_retract_entity_relation",
                payload={
                    "relation_id": str(relation_id),
                    "expected_version": expected_version,
                    "reason": reason,
                },
                execute=lambda: {
                    "ok": True,
                    **service.retract_relation(
                        relation_id=relation_id,
                        expected_version=expected_version,
                        reason=reason,
                        actor_id=actor_id,
                    ),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_merge_entities(
    survivor_entity_id: uuid.UUID,
    absorbed_entity_id: uuid.UUID,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Merge one duplicate identity into a selected survivor while preserving history."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_merge_entities",
                payload={
                    "survivor_entity_id": str(survivor_entity_id),
                    "absorbed_entity_id": str(absorbed_entity_id),
                },
                execute=lambda: {
                    "ok": True,
                    "entity": service.merge(
                        survivor_id=survivor_entity_id,
                        absorbed_id=absorbed_entity_id,
                        authority=IntentAuthority.EXPLICIT_USER,
                        actor_id=actor_id,
                    ).model_dump(mode="json"),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_rebind_entity_resolution(
    resolution_id: uuid.UUID,
    entity_id: uuid.UUID,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Correct a prior mention resolution and preserve both the old and new bindings."""
    try:
        with session_scope() as session:
            service = EntityService(session)
            return _execute_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
                operation_name="docket_rebind_entity_resolution",
                payload={"resolution_id": str(resolution_id), "entity_id": str(entity_id)},
                execute=lambda: {
                    "ok": True,
                    "resolution": service.rebind_resolution(
                        resolution_id=resolution_id,
                        entity_id=entity_id,
                        actor_id=actor_id,
                    ).model_dump(mode="json"),
                },
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_store_record(
    record_type: RecordType,
    canonical_identity: TermIdentity | CourseIdentity | GenericIdentity,
    title: str,
    data: TermData | CourseData | GenericRecordData,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Store an explicit source-backed assertion in Docket, not Hermes memory.

    Always call this for a current trusted Discord store/save/remember request, even when
    search found the canonical record. Materially equal existing records return
    ``matched_existing`` while attaching the current source provenance. Different data
    returns ``record_conflict`` without attaching provenance. Never copy the existing
    record into a retry to manufacture a match; use ``docket_update_record`` only for an
    explicitly authorized replacement. Search/get calls alone never persist provenance.
    """
    try:
        request = StoreRecordInput(
            record_type=record_type,
            canonical_identity=canonical_identity,
            title=title,
            data=data,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).store(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_record(record_key: str) -> dict[str, Any]:
    """Read one exact legacy Record by its canonical key or compatibility UUID."""
    try:
        with session_scope() as session:
            record = RecordService(session).get_by_lookup(record_key)
            return {"ok": True, "record": serialize_record(record)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_search_records(
    record_type: RecordType | None = None,
    query: str | None = None,
    status: str = "active",
    cursor: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search exact canonical Docket records before answering operational facts.

    This tool is read-only. Never claim a store/save/remember request succeeded from
    search results alone; call ``docket_store_record`` with the current trusted source.
    """
    try:
        record_status = RecordStatus(status)
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            records = RecordService(session).search(
                record_type=record_type,
                query=query,
                status=record_status,
                limit=limit + 1,
                offset=offset,
            )
            truncated = len(records) > limit
            page = records[:limit]
            return {
                "ok": True,
                "records": [serialize_record(record) for record in page],
                "count": len(page),
                "total_if_known": offset + len(page) if not truncated else None,
                "truncated": truncated,
                "cursor": str(offset + len(page)) if truncated else None,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_update_record(
    record_id: str,
    expected_version: int,
    data: dict[str, Any],
    request_key: str,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Replace validated record data using optimistic locking and idempotency.

    Read the canonical record and send a complete replacement only for an explicit
    operator correction. A materially identical replacement returns
    ``matched_existing`` with the unchanged version and canonical snapshot; it does not
    manufacture Calendar work.
    """
    try:
        request = UpdateRecordInput(
            record_id=uuid.UUID(record_id),
            expected_version=expected_version,
            data=data,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).update(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_archive_record(
    record_id: str,
    expected_version: int,
    request_key: str,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Soft-archive an unlinked canonical record; physical deletion is not exposed.

    A course with active Calendar links must instead use
    ``docket_apply_course_intent`` in ``drop`` mode so every provider series
    reaches a durable terminal cancellation before Docket archives the course.
    """
    try:
        request = ArchiveRecordInput(
            record_id=uuid.UUID(record_id),
            expected_version=expected_version,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).archive(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_restore_record(
    record_id: str,
    expected_version: int,
    request_key: str,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Reactivate one archived canonical identity without discarding its history.

    Restoring a course does not itself recreate Google Calendar series. After restore,
    call ``docket_apply_course_intent`` in ``sync`` mode; cancelled stable
    meeting links are then reused with fresh provider event identities.
    """
    try:
        request = RestoreRecordInput(
            record_id=uuid.UUID(record_id),
            expected_version=expected_version,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = RecordService(session).restore(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_accounts() -> dict[str, Any]:
    """List enabled Google accounts, active Calendar IDs, and every lane mapping.

    Use ``calendar_lanes`` rather than display-name guesses. An unprovisioned or failed
    lane is unavailable and must not silently fall back to ``unsorted``.
    """
    try:
        settings = get_settings()
        with session_scope() as session:
            accounts = AccountService(session).list_enabled_google()
            lane_service = CalendarLaneService(session, settings)
            return {
                "ok": True,
                "accounts": [
                    {
                        "ref": account.ref_id,
                        "provider": account.provider,
                        "external_account_id": account.external_account_id,
                        "display_name": account.display_name,
                        "email_address": account.email_address,
                        "capabilities": account.capabilities,
                        "calendar_ids": lane_service.calendar_ids(account.id),
                        "calendar_lanes": [
                            lane.model_dump(mode="json", exclude_none=True)
                            for lane in lane_service.list_lanes(account.id)
                        ],
                    }
                    for account in accounts
                ],
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_calendar_lanes(account_ref: SourceRef) -> dict[str, Any]:
    """List every configured Calendar lane and its provisioning state.

    This read is the authoritative source for Calendar destination selection. Academic,
    work, organizations, personal, and unsorted are managed defaults; explicitly created
    custom lanes may also be present.
    Explicit operator direction wins over entity defaults and bounded inference. A lane
    without an active ``calendar_id`` cannot receive events; do not invent an ID or fall
    back silently to another active lane.
    """
    try:
        with session_scope() as session:
            account = AccountService(session).require_google_ref(account_ref)
            lanes = CalendarLaneService(session, get_settings()).list_lanes(account.id)
            return {
                "ok": True,
                "calendar_lanes": [
                    lane.model_dump(mode="json", exclude_none=True) for lane in lanes
                ],
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_configure_calendar_lane(
    account_id: uuid.UUID,
    lane: CalendarLane,
    display_name: str,
    color_hex: str,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Provision or modify one Docket-managed Google Calendar lane.

    This is an external configuration mutation. Call it only when the current operator
    explicitly asks to create, rename, or recolor the lane. For creation, choose a stable
    lowercase slug and omit ``expected_version``. For rename/recolor, read lanes first and
    supply the current version. It never deletes a calendar or migrates events.
    """
    try:
        request = ConfigureCalendarLaneInput(
            account_id=account_id,
            lane=lane,
            expected_version=expected_version,
            display_name=display_name,
            color_hex=color_hex,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = CalendarLaneService(session, get_settings()).configure(request)
        return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_migrate_calendar_events(
    account_id: uuid.UUID,
    source_lane: CalendarLane,
    destination_lane: CalendarLane,
    expected_source_version: int,
    expected_destination_version: int,
    events: list[CalendarLaneEventSelection],
    reason: str,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Execute an explicit move of selected Calendar events or recurring series.

    Use this only for the operator's current explicit request. Resolve the event from
    a ``require_fresh`` Docket Calendar read, pass its provider ID, and use
    ``scope=series`` for recurring events. This trusted operator command queues the
    immutable items directly; no approval proposal is created. Docket updates its provider
    bindings only after Google confirms each move. Ambiguous events must remain unmoved
    until the operator clarifies them.
    """
    try:
        request = MigrateCalendarLaneEventsInput(
            account_id=account_id,
            source_lane=source_lane,
            destination_lane=destination_lane,
            expected_source_version=expected_source_version,
            expected_destination_version=expected_destination_version,
            events=events,
            reason=reason,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = CalendarLaneService(session, get_settings()).migrate_events(request)
        preview = result.get("preview")
        item_count = preview.get("item_count") if isinstance(preview, dict) else None
        return {
            "ok": True,
            **{key: value for key, value in result.items() if key != "preview"},
            "item_count": item_count,
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_delete_calendar_lane(
    account_id: uuid.UUID,
    lane: CalendarLane,
    expected_version: int,
    reason: str,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Execute an explicit deletion of one empty non-fallback Calendar lane.

    This action requires the operator's current explicit instruction but does not create an
    approval proposal. Read lanes first and supply the current version. Docket refuses the
    command while it knows of active events, refuses to delete ``unsorted``, and asks Google
    to verify the calendar is truly empty before deletion. Move or cancel remaining events
    first.
    """
    try:
        request = DeleteCalendarLaneInput(
            account_id=account_id,
            lane=lane,
            expected_version=expected_version,
            reason=reason,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = CalendarLaneService(session, get_settings()).delete_lane(request)
        return {"ok": True, **result}
    except Exception as exc:
        return _error(exc)


def _calendar_read_service() -> CalendarReadService:
    settings = get_settings()
    sync = CalendarSyncService(get_session_factory(), get_calendar_read_provider(), settings)
    return CalendarReadService(get_session_factory(), sync, settings)


def _refresh_active_calendar_lanes(account_id: uuid.UUID) -> None:
    with session_scope() as session:
        calendar_ids = CalendarLaneService(session, get_settings()).calendar_ids(account_id)
    read_service = _calendar_read_service()
    for calendar_id in calendar_ids:
        read_service.list_events(
            account_id=account_id,
            calendar_id=calendar_id,
            start=None,
            end=None,
            text_filter=None,
            limit=1,
            freshness="require_fresh",
        )


@mcp.tool()
def docket_list_calendar_events(
    account_ref: SourceRef,
    calendar_id: CalendarId,
    start: datetime | None = None,
    end: datetime | None = None,
    relative_day: CalendarRelativeDay | None = None,
    text_filter: CalendarTextFilter | None = None,
    cursor: str | None = None,
    limit: CalendarLimit = 25,
    freshness: CalendarFreshness = "prefer_cache",
    result_view: CalendarEventResultView = "occurrences",
) -> dict[str, Any]:
    """Read a bounded, redacted time range from Docket's Calendar cache.

    Supply both timezone-aware ``start`` and ``end``, or set ``relative_day`` to
    ``today`` or ``tomorrow``. Docket resolves relative days once in its configured
    timezone and returns the authoritative local date, timezone, and ``as_of`` instant;
    do not use a terminal or another clock to derive these bounds. Timed events include
    ``start_local`` and ``end_local`` in that configured timezone; use them directly and
    never call a terminal to convert event times. With no range input, the default is now
    through seven days. The maximum is 31 days. Use ``require_fresh`` for direct current,
    today, or tomorrow list/find requests because a healthy cache can still predate a
    newly added provider event by one synchronization interval. Results include cache
    freshness and each provider ``event_type``. Only ``default`` events are eligible for
    lane migration; leave other event types in place. Results never expose descriptions,
    attendees, conference data, credentials, or a raw Google client. ``require_fresh``
    may wait up to ten seconds for Docket's full bounded snapshot; it never promotes a
    partial requested subrange. Set ``result_view=series`` when selecting events
    for a lane migration: it returns one compact, directly reusable provider identity
    and scope per recurring series or one-time event instead of every occurrence.
    """
    try:
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            account_id = AccountService(session).require_google_ref(account_ref).id
        request = CalendarLookupInput(
            account_id=account_id,
            calendar_id=calendar_id,
            start=start,
            end=end,
            relative_day=relative_day,
            text_filter=text_filter,
            limit=limit,
            freshness=freshness,
            result_view=result_view,
        )
        result = _calendar_read_service().list_events(
            account_id=request.account_id,
            calendar_id=request.calendar_id,
            start=request.start,
            end=request.end,
            relative_day=request.relative_day,
            text_filter=request.text_filter,
            limit=request.limit,
            freshness=request.freshness,
            offset=offset,
        )
        return {"ok": True, **result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_calendar_sync_status(
    account_ref: SourceRef, calendar_id: CalendarId
) -> dict[str, Any]:
    """Return bounded Calendar-cache coverage, freshness, and a stable sync error code.

    This read never exposes credentials, provider cursors, or snapshot-generation IDs.
    """
    try:
        with session_scope() as session:
            account_id = AccountService(session).require_google_ref(account_ref).id
        result = _calendar_read_service().get_sync_status(account_id, calendar_id)
        return {"ok": True, "calendar_sync": result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_calendar_profile() -> dict[str, Any]:
    """Read Docket's Calendar proposal and unified-reminder defaults.

    The profile is local policy only: it cannot select a provider target, approve an
    action, or grant a provider write. Google popup is always available; the profile
    may additionally enable or disable Docket daily-thread delivery.
    """
    try:
        with session_scope() as session:
            profile = CalendarProfileService(session).get()
            return {"ok": True, "calendar_profile": profile.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_set_calendar_profile(
    profile: CalendarProfileInput,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Update local Calendar proposal policy from current trusted Discord context.

    This audited optimistic-locking write changes only proposal mode, conflict policy,
    and the canonical reminder defaults. Removing ``docket_queue`` disables existing
    Docket reminder rules while retaining Google popup delivery. It cannot choose a
    different calendar, approve a proposal, or contact Google Calendar.
    """
    try:
        request = SetCalendarProfileInput(
            **profile.model_dump(),
            expected_version=expected_version,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = CalendarProfileService(session).set(request)
            return {"ok": True, "calendar_profile": result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_reminder_rules(
    account_ref: SourceRef,
    calendar_id: CalendarId,
    enabled: bool | None = None,
    cursor: str | None = None,
    limit: CalendarLimit = 25,
) -> dict[str, Any]:
    """List bounded canonical reminder rules for the configured Calendar target.

    Use this read before updating or disabling a rule so its UUID and current version
    come from Docket rather than conversational memory or a past-session search. It
    never schedules, changes, disables, or sends a notification.
    """
    try:
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            account_id = AccountService(session).require_google_ref(account_ref).id
            rules = ReminderRuleService(session).list(
                account_id=account_id,
                calendar_id=calendar_id,
                enabled=enabled,
                limit=limit + 1,
                offset=offset,
            )
            lane = CalendarLaneService(session, get_settings()).require_active(
                account_id, calendar_id=calendar_id
            )
            rules = [{**rule, "lane_ref": lane.ref_id, "legacy": True} for rule in rules]
            truncated = len(rules) > limit
            page = rules[:limit]
            return {
                "ok": True,
                "reminder_rules": page,
                "count": len(page),
                "total_if_known": offset + len(page) if not truncated else None,
                "truncated": truncated,
                "cursor": str(offset + len(page)) if truncated else None,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_apply_calendar_intent(
    account_id: uuid.UUID,
    calendar_id: CalendarId,
    proposal: CalendarEventProposal,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    entity_bindings: list[EventEntityBindingInput] | None = None,
    context_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Apply one explicit standalone Calendar create, update, reminder change, or cancellation.

    For create or replacement input, select the event's stable ``calendar_lane`` first
    and pass the exact active ``calendar_id`` mapped to that lane by
    ``docket_list_calendar_lanes``. Explicit operator direction wins, then a stored
    entity's ``calendar_lane_default``, then bounded inference; genuinely ambiguous
    events use ``unsorted``. The tool rejects a mismatched lane and Calendar ID.
    Docket refreshes its complete bounded Calendar snapshot, resolves exact existing
    targets, rejects unsafe attendee-bearing events, detects overlaps, applies the
    stored Calendar profile, and derives an immutable formulation. Reminder plans
    always project to Google popup and project to Docket's due-date ISO queue thread
    only when ``docket_queue`` is enabled in the profile; an empty lead list disables
    the configured delivery. An omitted standalone timing timezone inherits Docket's
    configured ``DOCKET_TIMEZONE``; an explicit IANA timezone wins. A sufficiently
    ascertained command from the current operator executes directly. Exact overlaps
    are advisory and produce a conflict-resolution card instead of failing or silently
    choosing a winner.
    For one occurrence or a non-recurring event, use ``target_scope="event"`` with that
    event's ``provider_event_id``. For an entire Docket-owned recurring series, use
    ``target_scope="series"`` with the ``recurring_event_id`` returned by Calendar
    lookup; never pass an occurrence ID when the operator asked for the whole series.
    The tool never records approval itself. It either durably queues the authorized
    provider operation or returns a persistent Discord decision card when a genuine
    conflict remains.
    """
    try:
        _refresh_active_calendar_lanes(account_id)
        request = ProposeCalendarEventInput(
            account_id=account_id,
            calendar_id=calendar_id,
            proposal=proposal,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
            entity_bindings=entity_bindings,
            context_labels=context_labels,
        )
        with session_scope() as session:
            result = CalendarActionService(session).apply_explicit(request)
            return {"ok": True, **_model_calendar_intent_result(result)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_apply_course_intent(
    record_id: uuid.UUID,
    expected_record_version: int,
    mode: CourseReconciliationMode,
    account_id: uuid.UUID,
    calendar_id: CalendarId,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    reminder_plan: CalendarReminderPlanInput | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Reconcile one independent course record with its linked Calendar series.

    Course meetings always target the active ``academic`` lane. Read the lane registry
    and pass that lane's exact Calendar ID; Docket rejects another destination.
    ``sync`` compares stable meeting identities and derives create, update, cancel, and
    no-op effects. A fully synchronized course returns ``no_op`` without a card.
    ``drop`` requires an explicit reason and creates one destructive durable operation;
    Docket archives the course only after every linked active series is terminally
    cancelled. Omitted courses are never inferred as drops. After
    ``docket_restore_record``, ``sync`` reuses cancelled logical links with fresh Google
    event identities. The tool refreshes Calendar state and executes an explicit
    operator instruction directly. Only an unresolved Calendar conflict creates a
    resolution card.
    """
    try:
        _refresh_active_calendar_lanes(account_id)
        request = ProposeCourseReconciliationInput(
            record_id=record_id,
            expected_record_version=expected_record_version,
            mode=mode,
            account_id=account_id,
            calendar_id=calendar_id,
            reminder_plan=reminder_plan,
            reason=reason,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = CourseReconciliationService(session).apply_explicit(request)
            return {"ok": True, **_model_course_reconciliation_result(result)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_queue_items(
    status: QueueStatus | None = None,
    category: str | None = None,
    local_date: date | None = None,
    priority: QueuePriority | None = None,
    source_ref: SourceRef | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """List bounded canonical queue state; Discord cards are only projections of it."""
    try:
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            source_item_id = None
            if source_ref is not None:
                source_item_id = session.scalar(
                    select(SourceItem.id).where(SourceItem.ref_id == source_ref)
                )
                if source_item_id is None:
                    raise DocketError(
                        code="source_not_found",
                        message="The requested source reference does not exist.",
                        details={"source_ref": source_ref},
                    )
            items = QueueService(session).list(
                status=status,
                category=category,
                local_date=local_date,
                priority=priority,
                source_item_id=source_item_id,
                limit=limit + 1,
                offset=offset,
            )
            truncated = len(items) > limit
            page = items[:limit]
            return {
                "ok": True,
                "queue_items": page,
                "count": len(page),
                "total_if_known": offset + len(page) if not truncated else None,
                "truncated": truncated,
                "cursor": str(offset + len(page)) if truncated else None,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_queue_item(item_ref: str) -> dict[str, Any]:
    """Read an exact legacy queue item, or route a case_ ref to AttentionCase context."""
    try:
        with session_scope() as session:
            if item_ref.startswith("case_"):
                return IntelligenceService(
                    get_session_factory(), get_gmail_read_provider()
                ).get_case(item_ref)
            return {
                "ok": True,
                "queue_item": QueueService(session).get_by_legacy_ref(item_ref),
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_snooze_queue_item(
    queue_item_id: str,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    reason: str,
    snoozed_until: datetime | None = None,
    snooze_local_date: date | None = None,
) -> dict[str, Any]:
    """Snooze one pending item using optimistic locking and an explicit wake time.

    A local-date wake occurs at the configured local daily rollover hour. The
    operation is local and idempotent; it never mutates Gmail or Calendar.
    """
    try:
        request = SnoozeQueueItemInput(
            queue_item_id=uuid.UUID(queue_item_id),
            expected_version=expected_version,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
            reason=reason,
            snoozed_until=snoozed_until,
            snooze_local_date=snooze_local_date,
        )
        with session_scope() as session:
            result = QueueService(session).snooze(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_ignore_queue_item(
    queue_item_id: str,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    reason: str,
) -> dict[str, Any]:
    """Ignore one pending or failed queue item locally without mutating its source."""
    try:
        request = IgnoreQueueItemInput(
            queue_item_id=uuid.UUID(queue_item_id),
            expected_version=expected_version,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
            reason=reason,
        )
        with session_scope() as session:
            result = QueueService(session).ignore(request)
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_action(
    action_id: str,
    detail: Literal["status", "full"] = "status",
    wait_seconds: ActionWaitSeconds = 0,
) -> dict[str, Any]:
    """Read an action's compact status, optionally waiting for terminal execution.

    The default ``status`` detail omits the immutable preview and target bindings so
    routine execution polling stays small. Use ``full`` only when those details are
    specifically needed for diagnosis. Set ``wait_seconds`` after a queued direct
    command to replace repeated model-mediated polling; Docket returns early when the
    operation becomes terminal, or returns its latest state after at most 30 seconds.
    """
    try:
        action_uuid = uuid.UUID(action_id)
        deadline = time.monotonic() + wait_seconds
        while True:
            with session_scope() as session:
                action = ActionReadService(session).get(
                    action_uuid,
                    include_revision=detail == "full",
                )
            operation = action.get("operation")
            terminal = not isinstance(operation, dict) or operation.get("status") not in {
                "pending",
                "running",
            }
            if terminal or time.monotonic() >= deadline:
                return {
                    "ok": True,
                    "action": action,
                    "wait": {
                        "terminal": terminal,
                        "timed_out": bool(wait_seconds and not terminal),
                    },
                }
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_network_search(
    query: EntitySearchQuery,
    entity_kinds: list[
        Literal[
            "person",
            "organization",
            "institution",
            "place",
            "course",
            "course_section",
            "project",
        ]
    ]
    | None = None,
    cursor: str | None = None,
    limit: NetworkLimit = 25,
) -> dict[str, Any]:
    """Search registered context-graph entities and aliases with compact summaries."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).network_search(
                query=query,
                entity_kinds=list(entity_kinds) if entity_kinds is not None else None,
                cursor=cursor,
                limit=limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_person_context(person_ref: EntityRef) -> dict[str, Any]:
    """Read bounded identity, graph context, history, and provenance for one Person."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).person_context(person_ref)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_organization_context(organization_ref: EntityRef) -> dict[str, Any]:
    """Read bounded hierarchy, people, interactions, and routing context for one org."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).organization_context(organization_ref)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_query_people(
    affiliated_with: EntityRef | None = None,
    shares_course_with_operator: bool = False,
    known_through: str | None = None,
    relationship_type: str | None = None,
    current_role: str | None = None,
    interaction_recency_days: Annotated[int, Field(ge=0, le=36500)] | None = None,
    fact_constraints: dict[str, Any] | None = None,
    cursor: str | None = None,
    limit: NetworkLimit = 25,
) -> dict[str, Any]:
    """Run bounded structured queries over registered People and their graph context."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).query_people(
                affiliated_with=affiliated_with,
                shares_course_with_operator=shares_course_with_operator,
                known_through=known_through,
                relationship_type=relationship_type,
                current_role=current_role,
                interaction_recency_days=interaction_recency_days,
                fact_constraints=fact_constraints or {},
                cursor=cursor,
                limit=limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_network_neighborhood(
    root_ref: EntityRef,
    depth: NetworkDepth = 2,
    max_nodes: NetworkNodeLimit = 100,
) -> dict[str, Any]:
    """Traverse a bounded registered graph neighborhood to a hard maximum depth of 3."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).neighborhood(
                root_ref=root_ref,
                depth=depth,
                max_nodes=max_nodes,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_search_history(
    object_type: str | None = None,
    ref: str | None = None,
    conversation_ref: str | None = None,
    related_ref: str | None = None,
    tool_name: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search bounded semantic provenance and tool/audit history by public reference."""
    try:
        with session_scope() as session:
            return HistoryService(session).search(
                object_type=object_type,
                ref_id=ref,
                conversation_ref=conversation_ref,
                related_ref=related_ref,
                tool_name=tool_name,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                cursor=cursor,
                limit=limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_history_entry(
    ref: str,
    view: HistoryView = "summary",
    text_offset: int = 0,
    text_limit: int = 32768,
) -> dict[str, Any]:
    """Fetch one exact provenance object; verbatim text requires explicit audit view."""
    try:
        with session_scope() as session:
            return HistoryService(session).get_entry(
                ref,
                view=view,
                text_offset=text_offset,
                text_limit=text_limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_conflict(conflict_ref: ConflictRef) -> dict[str, Any]:
    """Read one Conflict with statement values, intervals, and allowed resolutions."""
    try:
        with session_scope() as session:
            service = ConflictService(session)
            return {"ok": True, **service.projection(service.get(conflict_ref))}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_intent_session(intent_session_ref: SessionRef) -> dict[str, Any]:
    """Read durable resolved and unresolved state for one multi-turn intent session."""
    try:
        with session_scope() as session:
            service = IntentSessionService(session)
            return {
                "ok": True,
                **service.projection(service.get(intent_session_ref)),
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_triage_case(case_ref: str) -> dict[str, Any]:
    """Read one bounded AttentionCase and its typed unresolved/resolved items."""
    try:
        return IntelligenceService(get_session_factory(), get_gmail_read_provider()).get_case(
            case_ref
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_commit_changeset(
    utterance_ref: UtteranceRef,
    statements: list[StatementInput],
    relations: list[StatementRelationInput],
    resolved_intent: dict[str, Any],
    blocking_clarifications: list[dict[str, Any]],
    content: ChangeSetContent | None,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    intent_session_ref: SessionRef | None = None,
    expected_session_version: int | None = None,
    changeset_ref: str | None = None,
    expected_changeset_version: int | None = None,
    semantic_options: list[SemanticOptionDraft] | None = None,
    semantic_request_ref: str | None = None,
    authority_scope_hash: str | None = None,
    precondition_hash: str | None = None,
) -> dict[str, Any]:
    """Commit resolved intent atomically; use *_change_id and exact case_/caserev_."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
            )
            return InteractiveAuthorityService(session).process_turn(
                utterance_ref=utterance_ref,
                request_key=request_key,
                actor_id=actor_id,
                intent_session_ref=intent_session_ref,
                expected_session_version=expected_session_version,
                statements=statements,
                relations=relations,
                resolved_intent_json=resolved_intent,
                blocking_clarifications=blocking_clarifications,
                semantic_options=semantic_options,
                content=content,
                changeset_ref=changeset_ref,
                expected_changeset_version=expected_changeset_version,
                semantic_request_ref=semantic_request_ref,
                authority_scope_hash=authority_scope_hash,
                precondition_hash=precondition_hash,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_resolve_conflict(
    utterance_ref: UtteranceRef,
    conflict_ref: ConflictRef,
    expected_conflict_version: int,
    resolution: Literal[
        "resolved_supersession",
        "resolved_scoped_coexistence",
        "resolved_retraction",
    ],
    chosen_interpretation: dict[str, Any],
    statements_superseded: list[StatementRef],
    statements_retained: list[StatementRef],
    effective_scope: dict[str, Any],
    canonical_effects: list[CanonicalChangeInput],
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    intent_session_ref: SessionRef | None = None,
    expected_session_version: int | None = None,
    expected_versions: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Resolve one Conflict from the current utterance through an atomic ChangeSet."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session,
                request_key=request_key,
                source=source,
                actor_id=actor_id,
            )
            conflict = ConflictService(session).get(conflict_ref)
            statement = StatementInput(
                statement_kind="conflict_resolution",
                subject_refs=conflict.subject_refs,
                predicate="conflict_resolution",
                value_json=chosen_interpretation,
                affected_fields=conflict.affected_fields,
                interpretation_json={"conflict_ref": conflict.ref_id},
                interpreter_version="docket-conflict-tool-v1",
            )
            resolution_request = ConflictResolve(
                conflict_ref=conflict.ref_id,
                expected_version=expected_conflict_version,
                authority_utterance_ref=utterance_ref,
                resolution=resolution,
                chosen_interpretation=chosen_interpretation,
                statements_superseded=statements_superseded,
                statements_retained=statements_retained,
                effective_scope=effective_scope,
                expected_versions=expected_versions or {},
                canonical_effects=canonical_effects,
            )
            return InteractiveAuthorityService(session).process_conflict_resolution(
                utterance_ref=utterance_ref,
                request_key=request_key,
                actor_id=actor_id,
                intent_session_ref=intent_session_ref,
                expected_session_version=expected_session_version,
                statement=statement,
                resolution=resolution_request,
            )
    except Exception as exc:
        return _error(exc)


# Frozen ontology cutover: legacy reads remain available during their documented
# compatibility window, but all new interactive mutations compile through one
# authenticated ChangeSet (or the specialized Conflict resolver above).
for _legacy_mutation_tool in (
    "docket_add_entity_alias",
    "docket_apply_calendar_intent",
    "docket_apply_course_intent",
    "docket_archive_record",
    "docket_configure_calendar_lane",
    "docket_create_entity",
    "docket_delete_calendar_lane",
    "docket_get_action",
    "docket_ignore_queue_item",
    "docket_merge_entities",
    "docket_migrate_calendar_events",
    "docket_rebind_entity_resolution",
    "docket_relate_entities",
    "docket_restore_record",
    "docket_retract_entity_relation",
    "docket_set_calendar_profile",
    "docket_snooze_queue_item",
    "docket_store_record",
    "docket_update_entity",
    "docket_update_entity_relation",
    "docket_update_record",
):
    mcp.remove_tool(_legacy_mutation_tool)

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.enums import IntentAuthority, RecordStatus
from docket.domain.errors import DocketError
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
from docket.schemas.calendar import (
    CalendarFreshness,
    CalendarLookupInput,
    CalendarProfileInput,
    CalendarRelativeDay,
    CalendarReminderPlanInput,
    SetCalendarProfileInput,
)
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
from docket.schemas.triage import EntityClass
from docket.services.accounts import AccountService
from docket.services.action_read import ActionReadService
from docket.services.calendar_actions import CalendarActionService
from docket.services.calendar_profile import CalendarProfileService
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService
from docket.services.course_reconciliation import CourseReconciliationService
from docket.services.entities import EntityService
from docket.services.queue import QueueService
from docket.services.records import RecordService, serialize_record
from docket.services.reminders import ReminderRuleService
from docket.services.source_context import validate_configured_discord_source

mcp = FastMCP(
    "docket",
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


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DocketError):
        return exc.as_dict()
    return {
        "ok": False,
        "error": {"code": "validation_error", "message": str(exc), "details": {}},
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


@mcp.tool()
def docket_resolve_entity(
    entity_class: EntityClass,
    mention: str,
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
                "relationships": relationships,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_create_entity(
    entity_class: EntityClass,
    canonical_name: str,
    attributes: dict[str, Any],
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Create or confirm one genuinely new canonical entity from current user intent."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            result = EntityService(session).create(
                entity_class=entity_class,
                canonical_name=canonical_name,
                attributes=attributes,
                authority=IntentAuthority.EXPLICIT_USER,
                actor_type="hermes",
                actor_id=actor_id,
            )
            return {"ok": True, "entity": result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_update_entity(
    entity_id: uuid.UUID,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    canonical_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Correct the name or attributes of one exact entity with optimistic versioning."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            result = EntityService(session).update(
                entity_id=entity_id,
                expected_version=expected_version,
                canonical_name=canonical_name,
                attributes=attributes,
                authority=IntentAuthority.EXPLICIT_USER,
                actor_id=actor_id,
            )
            return {"ok": True, "entity": result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_add_entity_alias(
    entity_id: uuid.UUID,
    alias: str,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Teach Docket an explicit alias for one exact canonical entity."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            result = EntityService(session).add_alias(
                entity_id=entity_id,
                alias=alias,
                authority=IntentAuthority.EXPLICIT_USER,
            )
            return {"ok": True, "entity": result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_relate_entities(
    subject_entity_id: uuid.UUID,
    predicate: str,
    object_entity_id: uuid.UUID,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one typed relationship between two exact canonical entities."""
    try:
        with session_scope() as session:
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            relation_id = EntityService(session).relate(
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                object_entity_id=object_entity_id,
                authority=IntentAuthority.EXPLICIT_USER,
                attributes=attributes,
            )
            return {"ok": True, "relation_id": str(relation_id)}
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
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            result = EntityService(session).merge(
                survivor_id=survivor_entity_id,
                absorbed_id=absorbed_entity_id,
                authority=IntentAuthority.EXPLICIT_USER,
                actor_id=actor_id,
            )
            return {"ok": True, "entity": result.model_dump(mode="json")}
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
            _validate_entity_write(
                session, request_key=request_key, source=source, actor_id=actor_id
            )
            result = EntityService(session).rebind_resolution(
                resolution_id=resolution_id,
                entity_id=entity_id,
                actor_id=actor_id,
            )
            return {"ok": True, "resolution": result.model_dump(mode="json")}
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
def docket_get_record(record_id: str) -> dict[str, Any]:
    """Read exact canonical Docket state by UUID; this read-only tool persists no source."""
    try:
        with session_scope() as session:
            record = RecordService(session).get(uuid.UUID(record_id))
            return {"ok": True, "record": serialize_record(record)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_search_records(
    record_type: RecordType | None = None,
    query: str | None = None,
    status: str = "active",
    limit: int = 20,
) -> dict[str, Any]:
    """Search exact canonical Docket records before answering operational facts.

    This tool is read-only. Never claim a store/save/remember request succeeded from
    search results alone; call ``docket_store_record`` with the current trusted source.
    """
    try:
        record_status = RecordStatus(status)
        with session_scope() as session:
            records = RecordService(session).search(
                record_type=record_type,
                query=query,
                status=record_status,
                limit=limit,
            )
            return {"ok": True, "records": [serialize_record(record) for record in records]}
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
    """List enabled Docket-owned Google accounts and capabilities for explicit selection."""
    try:
        settings = get_settings()
        with session_scope() as session:
            accounts = AccountService(session).list_enabled_google()
            return {
                "ok": True,
                "accounts": [
                    {
                        "account_id": str(account.id),
                        "provider": account.provider,
                        "external_account_id": account.external_account_id,
                        "display_name": account.display_name,
                        "email_address": account.email_address,
                        "capabilities": account.capabilities,
                        "calendar_ids": [settings.google_calendar_id],
                    }
                    for account in accounts
                ],
            }
    except Exception as exc:
        return _error(exc)


def _calendar_read_service() -> CalendarReadService:
    settings = get_settings()
    sync = CalendarSyncService(get_session_factory(), get_calendar_read_provider(), settings)
    return CalendarReadService(get_session_factory(), sync, settings)


@mcp.tool()
def docket_list_calendar_events(
    account_id: uuid.UUID,
    calendar_id: CalendarId,
    start: datetime | None = None,
    end: datetime | None = None,
    relative_day: CalendarRelativeDay | None = None,
    text_filter: CalendarTextFilter | None = None,
    limit: CalendarLimit = 100,
    freshness: CalendarFreshness = "prefer_cache",
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
    freshness and never expose descriptions, attendees, conference data, credentials,
    or a raw Google client. ``require_fresh`` may wait up to ten seconds for Docket's
    full bounded snapshot; it never promotes a partial requested subrange.
    """
    try:
        request = CalendarLookupInput(
            account_id=account_id,
            calendar_id=calendar_id,
            start=start,
            end=end,
            relative_day=relative_day,
            text_filter=text_filter,
            limit=limit,
            freshness=freshness,
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
        )
        return {"ok": True, **result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_calendar_sync_status(
    account_id: uuid.UUID, calendar_id: CalendarId
) -> dict[str, Any]:
    """Return bounded Calendar-cache coverage, freshness, and a stable sync error code.

    This read never exposes credentials, provider cursors, or snapshot-generation IDs.
    """
    try:
        result = _calendar_read_service().get_sync_status(account_id, calendar_id)
        return {"ok": True, "calendar_sync": result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_calendar_profile() -> dict[str, Any]:
    """Read Docket's Calendar proposal and unified-reminder defaults.

    The profile is local policy only: it cannot select a provider target, approve an
    action, or grant a provider write. Reminder delivery always includes both Google
    popup and the Docket daily queue thread.
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
    and the canonical reminder defaults. It cannot split Google and Docket delivery,
    choose a different calendar, approve a proposal, or contact Google Calendar.
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
    account_id: uuid.UUID,
    calendar_id: CalendarId,
    enabled: bool | None = None,
    limit: CalendarLimit = 100,
) -> dict[str, Any]:
    """List bounded canonical reminder rules for the configured Calendar target.

    Use this read before updating or disabling a rule so its UUID and current version
    come from Docket rather than conversational memory or a past-session search. It
    never schedules, changes, disables, or sends a notification.
    """
    try:
        with session_scope() as session:
            rules = ReminderRuleService(session).list(
                account_id=account_id,
                calendar_id=calendar_id,
                enabled=enabled,
                limit=limit,
            )
            return {"ok": True, "reminder_rules": rules}
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

    Docket refreshes its complete bounded Calendar snapshot, resolves exact existing
    targets, rejects unsafe attendee-bearing events, detects overlaps, applies the
    stored Calendar profile, and derives an immutable formulation. Reminder plans
    always project to both Google popup and Docket's due-date ISO queue thread; an empty
    lead list disables both. An omitted standalone timing timezone inherits Docket's
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
        _calendar_read_service().list_events(
            account_id=account_id,
            calendar_id=calendar_id,
            start=None,
            end=None,
            text_filter=None,
            limit=1,
            freshness="require_fresh",
        )
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
        _calendar_read_service().list_events(
            account_id=account_id,
            calendar_id=calendar_id,
            start=None,
            end=None,
            text_filter=None,
            limit=1,
            freshness="require_fresh",
        )
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
    source_item_id: uuid.UUID | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List bounded canonical queue state; Discord cards are only projections of it."""
    try:
        with session_scope() as session:
            items = QueueService(session).list(
                status=status,
                category=category,
                local_date=local_date,
                priority=priority,
                source_item_id=source_item_id,
                limit=limit,
            )
            return {"ok": True, "queue_items": items}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_queue_item(queue_item_id: str) -> dict[str, Any]:
    """Get canonical queue state, lifecycle fields, and dated projection identities."""
    try:
        with session_scope() as session:
            return {
                "ok": True,
                "queue_item": QueueService(session).get(uuid.UUID(queue_item_id)),
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
def docket_get_action(action_id: str) -> dict[str, Any]:
    """Read a redacted action, immutable preview, approval, and operation status."""
    try:
        with session_scope() as session:
            return {"ok": True, "action": ActionReadService(session).get(uuid.UUID(action_id))}
    except Exception as exc:
        return _error(exc)

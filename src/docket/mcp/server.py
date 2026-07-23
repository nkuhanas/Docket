import uuid
from datetime import date, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.enums import RecordStatus
from docket.domain.errors import DocketError
from docket.providers.google.runtime import get_calendar_read_provider
from docket.schemas.actions import (
    CalendarActionType,
    CalendarMeetingActionParameters,
    ProposalResult,
    ProposeActionInput,
)
from docket.schemas.calendar import (
    CalendarFreshness,
    CalendarLookupInput,
    CalendarRelativeDay,
    DisableReminderRuleInput,
    ReminderScope,
    SetReminderRuleInput,
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
    StoreRecordInput,
    TermData,
    TermIdentity,
    UpdateRecordInput,
)
from docket.services.accounts import AccountService
from docket.services.actions import ActionService
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService
from docket.services.queue import QueueService
from docket.services.records import RecordService, serialize_record
from docket.services.reminders import ReminderRuleService

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
ReminderLeadSeconds = Annotated[int, Field(ge=0, le=2_678_400)]


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
    """Replace validated record data using optimistic locking and idempotency."""
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
    """Soft-archive a canonical record; physical deletion is not exposed."""
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
def docket_set_reminder_rule(
    account_id: uuid.UUID,
    calendar_id: CalendarId,
    scope: ReminderScope,
    lead_seconds: ReminderLeadSeconds,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    provider_event_id: CalendarId | None = None,
    rule_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Create or update an explicit deterministic Calendar reminder rule.

    Docket routes delivery to the due-date ISO thread under its configured queue; the
    model cannot select a Discord destination. This local, audited write schedules
    future deterministic notifications, cannot send arbitrary immediate text, and never
    infers a standing rule from conversation or source content.
    """
    try:
        request = SetReminderRuleInput(
            rule_id=rule_id,
            expected_version=expected_version,
            account_id=account_id,
            calendar_id=calendar_id,
            scope=scope,
            provider_event_id=provider_event_id,
            lead_seconds=lead_seconds,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = ReminderRuleService(session).set(request)
            return {"ok": True, **result.model_dump(mode="json")}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_disable_reminder_rule(
    rule_id: uuid.UUID,
    expected_version: int,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
    reason: str,
) -> dict[str, Any]:
    """Disable one explicit reminder rule using optimistic locking and idempotency.

    Pending notifications for the rule are cancelled locally; this cannot delete or
    modify the corresponding Google Calendar event.
    """
    try:
        request = DisableReminderRuleInput(
            rule_id=rule_id,
            expected_version=expected_version,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
            reason=reason,
        )
        with session_scope() as session:
            result = ReminderRuleService(session).disable(request)
            return {"ok": True, **result.model_dump(mode="json")}
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

    A local-date wake occurs at the configured 07:00 Los Angeles rollover. The
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
def docket_propose_action(
    action_type: CalendarActionType,
    record_id: str,
    expected_record_version: int,
    account_id: str,
    parameters: CalendarMeetingActionParameters,
    request_key: DiscordRequestKey,
    source: RecordSourceInput,
    actor_id: DiscordId,
) -> dict[str, Any]:
    """Propose a typed Calendar write from trusted Discord context.

    Docket derives risk, exact executable parameters, immutable preview, hashes,
    target versions, and approval expiry. Normal approval occurs only through the
    persistent Approve/Reject buttons on the projected card in today's ISO-dated
    Docket queue thread. Do not instruct the operator to type an approval code; that
    compatibility path is break-glass only and is not returned to the model. This tool
    never records or consumes an approval and never contacts Google Calendar.
    """
    try:
        request = ProposeActionInput(
            action_type=action_type,
            record_id=uuid.UUID(record_id),
            expected_record_version=expected_record_version,
            account_id=uuid.UUID(account_id),
            parameters=parameters,
            request_key=request_key,
            source=source,
            actor_id=actor_id,
        )
        with session_scope() as session:
            result = ActionService(session).propose(request)
            return {"ok": True, **_model_proposal_result(result)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_action(action_id: str) -> dict[str, Any]:
    """Read a redacted action, immutable preview, approval, and operation status."""
    try:
        with session_scope() as session:
            return {"ok": True, "action": ActionService(session).get(uuid.UUID(action_id))}
    except Exception as exc:
        return _error(exc)

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, cast

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.database import get_session_factory, session_scope
from docket.domain.errors import DocketError
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.models import ReminderPlan
from docket.providers.google.gmail_runtime import get_gmail_read_provider
from docket.providers.google.runtime import get_calendar_read_provider
from docket.schemas.authority import (
    CanonicalChangeInput,
    ConflictRef,
    ConflictResolve,
    OperatorChangeSetContent,
    SemanticOptionDraft,
    SessionRef,
    SourceRef,
    StatementInput,
    StatementRef,
    StatementRelationInput,
    UtteranceRef,
)
from docket.schemas.calendar import (
    CalendarEventResultView,
    CalendarFreshness,
    CalendarRelativeDay,
)
from docket.schemas.common import HistoryObjectType, ProviderAccountRef, PublicRef
from docket.schemas.registry import EntityRef
from docket.services.accounts import AccountService
from docket.services.attachment_evidence import (
    AttachmentEvidenceService,
    AttachmentTextService,
)
from docket.services.calendar_lanes import CalendarLaneService
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService
from docket.services.conflicts import ConflictService
from docket.services.history import HistoryService
from docket.services.intelligence import IntelligenceService
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.network import NetworkQueryService
from docket.services.tracked_context import TrackedContextService

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

RequestKey = Annotated[str, Field(min_length=1, max_length=512)]
CalendarId = Annotated[str, Field(min_length=1, max_length=1024)]
CalendarLimit = Annotated[int, Field(ge=1, le=100)]
CalendarTextFilter = Annotated[str, Field(max_length=200)]
NetworkLimit = Annotated[int, Field(ge=1, le=100)]
NetworkDepth = Annotated[int, Field(ge=1, le=3)]
NetworkNodeLimit = Annotated[int, Field(ge=1, le=100)]
AttachmentTextBytes = Annotated[int, Field(ge=256, le=8192)]
AttachmentPageLimit = Annotated[int, Field(ge=1, le=5)]
AttachmentTextCursor = Annotated[str, Field(min_length=3, max_length=64)]
EntitySearchQuery = Annotated[str, Field(min_length=1, max_length=256)]
HistoryView = Literal["summary", "audit"]
ProviderAccountView = Literal["summary", "details"]
CalendarLaneView = Literal["summary", "routing", "audit"]
CalendarEventDetail = Literal["summary", "details"]


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


def _offset_cursor(cursor: str | None) -> int:
    try:
        offset = int(cursor or "0")
    except ValueError as exc:
        raise DocketError(
            code="invalid_cursor", message="Cursor must be a non-negative offset."
        ) from exc
    if offset < 0:
        raise DocketError(code="invalid_cursor", message="Cursor must be non-negative.")
    return offset


def _calendar_read_service() -> CalendarReadService:
    settings = get_settings()
    sync = CalendarSyncService(get_session_factory(), get_calendar_read_provider(), settings)
    return CalendarReadService(get_session_factory(), sync, settings)


def _attachment_text_service(session: Session) -> AttachmentTextService:
    settings = get_settings()
    evidence = AttachmentEvidenceService(
        session,
        encryption_key=settings.attachment_encryption_key(),
        encryption_key_ref=settings.attachment_encryption_key_ref,
        max_attachment_bytes=settings.attachment_max_bytes,
        max_total_bytes=settings.attachment_total_max_bytes,
    )
    return AttachmentTextService(evidence)


def _calendar_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("is_all_day"):
        timing = {
            "kind": "all_day",
            "start_date": event.get("start_date"),
            "end_date": event.get("end_date"),
            "timezone": event.get("timezone"),
        }
    else:
        timing = {
            "kind": "timed",
            "start_local": event.get("start_local"),
            "end_local": event.get("end_local"),
            "timezone": event.get("local_timezone"),
        }
    return {
        key: value
        for key, value in {
            "ref": event.get("ref"),
            "lane_ref": event.get("lane_ref"),
            "calendar_id": event.get("calendar_id"),
            "object_type": event.get("object_type"),
            "semantic_role": event.get("semantic_role"),
            "status": event.get("status"),
            "summary": event.get("summary"),
            "location": event.get("location"),
            "timing": {key: value for key, value in timing.items() if value is not None},
            "series": (
                {
                    "scope": event.get("scope"),
                    "occurrences_in_range": event.get("occurrences_in_range"),
                }
                if "scope" in event
                else None
            ),
        }.items()
        if value is not None
    }


def _calendar_events_summary(result: dict[str, Any]) -> dict[str, Any]:
    raw_events = result.get("events", [])
    freshness_by_calendar = result.get("freshness_by_calendar")
    if isinstance(freshness_by_calendar, dict):
        freshness_values = list(freshness_by_calendar.values())
        freshness = {
            "calendar_count": len(freshness_values),
            "stale": any(bool(item.get("stale")) for item in freshness_values),
            "covered": all(bool(item.get("covered")) for item in freshness_values),
        }
    else:
        raw_freshness = result.get("freshness", {})
        freshness = {
            "calendar_count": 1,
            "stale": bool(raw_freshness.get("stale")),
            "covered": bool(raw_freshness.get("covered")),
        }
    freshness.update(
        {
            "refresh_pending": bool(result.get("refresh_pending")),
            "refresh_disabled": bool(result.get("refresh_disabled")),
        }
    )
    return {
        "ok": True,
        "account_ref": result.get("account_ref"),
        "range": {
            "start": result.get("range_start"),
            "end": result.get("range_end"),
            "resolution": result.get("range_resolution"),
        },
        "result_view": result.get("result_view"),
        "detail": "summary",
        "items": [_calendar_event_summary(event) for event in raw_events],
        "count": result.get("count", len(raw_events)),
        "total_if_known": result.get("total_if_known"),
        "truncated": bool(result.get("truncated")),
        "cursor": result.get("cursor"),
        "freshness": freshness,
    }


@mcp.tool()
def docket_search_entities(
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
    """Search registered Entities and aliases with bounded compact summaries."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).network_search(
                query=query,
                entity_kinds=cast(list[str] | None, entity_kinds),
                cursor=cursor,
                limit=limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_read_attachment_text(
    source_ref: SourceRef,
    cursor: AttachmentTextCursor | None = None,
    max_text_bytes: AttachmentTextBytes = 8192,
    page_limit: AttachmentPageLimit = 3,
) -> dict[str, Any]:
    """Read bounded untrusted PDF text with exact source-fragment coordinates."""
    try:
        with session_scope() as session:
            return _attachment_text_service(session).read_pdf_text(
                source_ref=source_ref,
                cursor=cursor,
                max_text_bytes=max_text_bytes,
                page_limit=page_limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_person_context(person_ref: EntityRef) -> dict[str, Any]:
    """Read bounded graph context and provenance for one registered Person."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).person_context(person_ref)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_organization_or_institution_context(
    organization_or_institution_ref: EntityRef,
) -> dict[str, Any]:
    """Read one Organization or Institution hierarchy and relational context."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).organization_context(
                organization_or_institution_ref
            )
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
    """Run bounded structured queries over registered People."""
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
def docket_get_context_neighborhood(
    root_ref: PublicRef,
    depth: NetworkDepth = 2,
    max_nodes: NetworkNodeLimit = 100,
) -> dict[str, Any]:
    """Traverse bounded Entity, Item, Task, Time, and Event context."""
    try:
        with session_scope() as session:
            return NetworkQueryService(session).context_neighborhood(
                root_ref=root_ref, depth=depth, max_nodes=max_nodes
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_query_items(
    text: str | None = None,
    kind: str | None = None,
    context_entity_ref: EntityRef | None = None,
    parent_item_ref: str | None = None,
    temporal_role: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    has_open_task: bool | None = None,
    source_ref: str | None = None,
    cursor: str | None = None,
    limit: NetworkLimit = 25,
) -> dict[str, Any]:
    """Search bounded tracked Items by context, time, source, and work facets."""
    try:
        with session_scope() as session:
            return TrackedContextService(session).query_items(
                text=text,
                kind=kind,
                context_entity_ref=context_entity_ref,
                parent_item_ref=parent_item_ref,
                temporal_role=temporal_role,
                date_from=date_from,
                date_to=date_to,
                has_open_task=has_open_task,
                source_ref=source_ref,
                cursor=cursor,
                limit=limit,
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_item_context(item_ref: str) -> dict[str, Any]:
    """Read one Item with its Entity, Task, Time, Event, source, and provenance facets."""
    try:
        with session_scope() as session:
            return TrackedContextService(session).item_context(item_ref)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_attention_case(case_ref: str) -> dict[str, Any]:
    """Read one bounded AttentionCase and its typed CaseItems."""
    try:
        return IntelligenceService(get_session_factory(), get_gmail_read_provider()).get_case(
            case_ref
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_search_history(
    object_type: HistoryObjectType | None = None,
    ref: str | None = None,
    conversation_ref: str | None = None,
    related_ref: str | None = None,
    tool_name: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Search bounded semantic provenance and accountability history."""
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
    text_offset: Annotated[int, Field(ge=0)] = 0,
    text_limit: Annotated[int, Field(ge=1, le=65536)] = 32768,
) -> dict[str, Any]:
    """Fetch one exact referenced history object; text requires audit view."""
    try:
        with session_scope() as session:
            return HistoryService(session).get_entry(
                ref, view=view, text_offset=text_offset, text_limit=text_limit
            )
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_conflict(conflict_ref: ConflictRef) -> dict[str, Any]:
    """Read one Conflict with allowed resolution actions."""
    try:
        with session_scope() as session:
            service = ConflictService(session)
            return {"ok": True, **service.projection(service.get(conflict_ref))}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_intent_session(intent_session_ref: SessionRef) -> dict[str, Any]:
    """Read durable semantic and commit state for one IntentSession."""
    try:
        with session_scope() as session:
            service = IntentSessionService(session)
            return {"ok": True, **service.projection(service.get(intent_session_ref))}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_provider_accounts(
    view: ProviderAccountView = "summary",
) -> dict[str, Any]:
    """List compact enabled provider accounts; request details for raw bindings."""
    try:
        with session_scope() as session:
            lane_service = CalendarLaneService(session, get_settings())
            accounts = AccountService(session).list_enabled_google()
            items = []
            for account in accounts:
                calendar_ids = lane_service.calendar_ids(account.id)
                item = {
                    "ref": account.ref_id,
                    "display_name": account.display_name,
                    "capabilities": account.capabilities,
                    "calendar_binding_count": len(calendar_ids),
                }
                if view == "details":
                    item.update(
                        {
                            "provider": account.provider,
                            "email_address": account.email_address,
                            "calendar_ids": calendar_ids,
                        }
                    )
                items.append(item)
            return {
                "ok": True,
                "view": view,
                "items": items,
                "count": len(items),
                "total_if_known": len(items),
                "truncated": False,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_calendar_lanes(
    account_ref: ProviderAccountRef,
    view: CalendarLaneView = "summary",
) -> dict[str, Any]:
    """List compact Calendar lanes; request routing or audit fields explicitly."""
    try:
        with session_scope() as session:
            account = AccountService(session).require_google_ref(account_ref)
            lanes = CalendarLaneService(session, get_settings()).list_lanes(account.id)
            items: list[dict[str, Any]] = []
            for lane in lanes:
                item = {
                    "ref": lane.ref,
                    "lane": lane.lane,
                    "display_name": lane.display_name,
                    "status": lane.status,
                    "enabled": lane.enabled,
                    "version": lane.version,
                }
                if view in {"routing", "audit"}:
                    item.update(
                        {
                            "calendar_id": lane.calendar_id,
                            "operator_policy_text": lane.operator_policy_text,
                            "priority": lane.priority,
                        }
                    )
                if view == "audit":
                    item.update(
                        {
                            "color_hex": lane.color_hex,
                            "metadata_json": lane.metadata_json,
                            "basis_refs": lane.basis_refs,
                            "decision_refs": lane.decision_refs,
                            "source_refs": lane.source_refs,
                            "created_by_changeset_ref": (
                                lane.created_by_changeset_ref
                            ),
                        }
                    )
                items.append(item)
            return {
                "ok": True,
                "view": view,
                "items": items,
                "count": len(items),
                "total_if_known": len(items),
                "truncated": False,
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_provider_calendar_events(
    account_ref: ProviderAccountRef,
    calendar_id: CalendarId | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    relative_day: CalendarRelativeDay | None = None,
    text_filter: CalendarTextFilter | None = None,
    cursor: str | None = None,
    limit: CalendarLimit = 25,
    freshness: CalendarFreshness = "prefer_cache",
    result_view: CalendarEventResultView = "occurrences",
    detail: CalendarEventDetail = "summary",
) -> dict[str, Any]:
    """Read one globally ordered Calendar range as compact semantic summaries.

    Request details only when provider recurrence or reminder metadata is needed.
    This tool never mutates a provider.
    """
    try:
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            account = AccountService(session).require_google_ref(account_ref)
            calendar_ids = CalendarLaneService(session, get_settings()).calendar_ids(account.id)
            account_id = account.id
        reader = _calendar_read_service()
        if calendar_id is None:
            result = reader.list_events_across_calendars(
                account_id=account_id,
                calendar_ids=calendar_ids,
                start=start,
                end=end,
                relative_day=relative_day,
                text_filter=text_filter,
                limit=limit,
                freshness=freshness,
                result_view=result_view,
                offset=offset,
            )
        else:
            if calendar_id not in calendar_ids:
                raise DocketError(
                    code="calendar_lane_unavailable",
                    message="Calendar ID is not bound to an active Docket lane.",
                )
            result = reader.list_events(
                account_id=account_id,
                calendar_id=calendar_id,
                start=start,
                end=end,
                relative_day=relative_day,
                text_filter=text_filter,
                limit=limit,
                freshness=freshness,
                result_view=result_view,
                offset=offset,
            )
        if detail == "summary":
            return _calendar_events_summary(result)
        return {"ok": True, "detail": "details", **result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_get_calendar_sync_status(
    account_ref: ProviderAccountRef, calendar_id: CalendarId
) -> dict[str, Any]:
    """Read provider Calendar cache coverage and freshness."""
    try:
        with session_scope() as session:
            account_id = AccountService(session).require_google_ref(account_ref).id
        result = _calendar_read_service().get_sync_status(account_id, calendar_id)
        return {"ok": True, "calendar_sync": result}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_list_reminder_plans(
    subject_ref: PublicRef | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """List canonical ReminderPlans, optionally scoped to one Event or Time."""
    try:
        offset = _offset_cursor(cursor)
        with session_scope() as session:
            query = select(ReminderPlan).where(ReminderPlan.canonical_status == "active")
            if subject_ref is not None:
                query = query.where(ReminderPlan.subject_ref == subject_ref)
            rows = list(
                session.scalars(query.order_by(ReminderPlan.created_at, ReminderPlan.ref_id))
            )
            selected = rows[offset : offset + limit]
            items = [
                {
                    "ref": row.ref_id,
                    "subject_ref": row.subject_ref,
                    "delivery_channels": row.delivery_channels,
                    "lead_seconds": row.lead_seconds,
                    "date_trigger_local_time": row.date_trigger_local_time,
                    "timezone": row.timezone,
                    "basis_refs": row.basis_refs,
                    "version": row.version,
                }
                for row in selected
            ]
            next_offset = offset + len(selected)
            return {
                "ok": True,
                "items": items,
                "count": len(items),
                "total_if_known": len(rows),
                "truncated": next_offset < len(rows),
                **({"cursor": str(next_offset)} if next_offset < len(rows) else {}),
            }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def docket_commit_changeset(
    utterance_ref: UtteranceRef,
    statements: list[StatementInput],
    relations: list[StatementRelationInput],
    resolved_intent: dict[str, Any],
    blocking_clarifications: list[dict[str, Any]],
    content: OperatorChangeSetContent | None,
    request_key: RequestKey,
    intent_session_ref: SessionRef | None = None,
    expected_session_version: int | None = None,
    changeset_ref: str | None = None,
    expected_changeset_version: int | None = None,
    semantic_options: list[SemanticOptionDraft] | None = None,
    semantic_request_ref: str | None = None,
    authority_scope_hash: str | None = None,
    precondition_hash: str | None = None,
) -> dict[str, Any]:
    """Compile and atomically commit one authenticated Operator turn.

    Reference persistent objects with ``*_ref``. Reference objects created in
    this same ChangeSet with ``*_change_id``; every dependency is validated
    before any canonical mutation begins. Under progressive disclosure, request
    this schema with only the exact discriminated ``mutation_types`` required by
    the current semantic request.
    """
    try:
        with session_scope() as session:
            return InteractiveAuthorityService(session).process_turn(
                utterance_ref=utterance_ref,
                request_key=request_key,
                actor_id=str(get_settings().operator_discord_user_id),
                intent_session_ref=intent_session_ref,
                expected_session_version=expected_session_version,
                statements=statements,
                relations=relations,
                resolved_intent_json=resolved_intent,
                blocking_clarifications=blocking_clarifications,
                semantic_options=semantic_options,
                content=content.to_internal() if content is not None else None,
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
    request_key: RequestKey,
    intent_session_ref: SessionRef | None = None,
    expected_session_version: int | None = None,
    expected_versions: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Resolve one Conflict through the same atomic ChangeSet service path."""
    try:
        with session_scope() as session:
            conflict = ConflictService(session).get(conflict_ref)
            statement = StatementInput(
                statement_kind="conflict_resolution",
                subject_refs=conflict.subject_refs,
                predicate="conflict_resolution",
                value_json=chosen_interpretation,
                affected_fields=conflict.affected_fields,
                interpretation_json={"conflict_ref": conflict.ref_id},
                interpreter_version="docket-conflict-tool-v2",
            )
            request = ConflictResolve(
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
                actor_id=str(get_settings().operator_discord_user_id),
                intent_session_ref=intent_session_ref,
                expected_session_version=expected_session_version,
                statement=statement,
                resolution=request,
            )
    except Exception as exc:
        return _error(exc)

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    CalendarEventCache,
    CanonicalEvent,
    DiscordProjection,
    Entity,
    EventObservation,
    OutboxEvent,
    ProviderEventBinding,
    QueueItem,
    QueueItemSource,
    SemanticCandidate,
    SourceItem,
)
from docket.models.base import utc_now
from docket.schemas.actions import (
    CancelCalendarEventProposal,
    CreateCalendarEventProposal,
    InferredCalendarEventInput,
    UpdateCalendarEventProposal,
)
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.brief_projection import (
    morning_brief_projection_for_queue_item,
    projection_refresh_target,
)
from docket.services.calendar_actions import CalendarActionService, _occurrence_intervals
from docket.services.calendar_lanes import CalendarLaneService
from docket.services.queue import queue_projection_date

_SPACE = re.compile(r"\s+")


def _normalized_title(value: str) -> str:
    return _SPACE.sub(" ", value.casefold().strip())


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _event_fingerprint(event: dict[str, Any]) -> str:
    return sha256_json(
        {
            "title": _normalized_title(str(event.get("title") or "")),
            "timing": event.get("timing"),
            "recurrence": event.get("recurrence"),
            "location": event.get("location"),
        }
    )


def _cache_snapshot(event: CalendarEventCache) -> dict[str, Any]:
    return {
        "status": event.status,
        "summary": event.summary,
        "location": event.location,
        "is_all_day": event.is_all_day,
        "start_at": event.start_at.isoformat() if event.start_at is not None else None,
        "end_at": event.end_at.isoformat() if event.end_at is not None else None,
        "start_date": event.start_date.isoformat() if event.start_date is not None else None,
        "end_date": event.end_date.isoformat() if event.end_date is not None else None,
        "timezone": event.timezone,
        "recurrence_kind": event.recurrence_kind,
        "provider_reminders": dict(event.provider_reminders),
    }


def _candidate_entity_refs(candidate: SemanticCandidate) -> list[dict[str, Any]]:
    """Return only identities that belong in the eventual event decision.

    Known entities may be retained as context. A required unknown entity is a
    provisional registration bundled with the event and is activated only
    after the provider operation succeeds. Optional unresolved mentions remain
    source provenance and never become registry objects or proposal gates.
    """

    raw_resolutions = candidate.fields.get("entity_resolutions")
    if not isinstance(raw_resolutions, list):
        return []
    refs: list[dict[str, Any]] = []
    for raw in raw_resolutions:
        if not isinstance(raw, dict) or raw.get("entity_id") is None:
            continue
        state = raw.get("state")
        required = bool(raw.get("required", False))
        if state == "resolved":
            registration = "existing"
        elif state == "provisional" and required:
            registration = "register_with_event"
        else:
            continue
        refs.append(
            {
                "entity_id": str(raw["entity_id"]),
                "entity_class": str(raw.get("entity_class") or "organization"),
                "canonical_name": str(raw.get("canonical_name") or raw.get("mention") or ""),
                "role": raw.get("role"),
                "resolution_id": raw.get("resolution_id"),
                "registration_disposition": registration,
            }
        )
    return refs


class CanonicalEventService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def correlate(self, candidate: SemanticCandidate) -> list[CanonicalEvent]:
        correlatable_statuses = (
            ("proposed", "active", "cancelled")
            if candidate.mutation == "cancel"
            else ("proposed", "active")
        )
        fields = candidate.fields
        correlation = fields.get("correlation")
        correlation = correlation if isinstance(correlation, dict) else {}
        provider_event_id = correlation.get("provider_event_id")
        if isinstance(provider_event_id, str):
            source = self.session.get(SourceItem, candidate.source_item_id)
            binding = self.session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.provider_event_id == provider_event_id,
                    *(
                        (ProviderEventBinding.account_id == source.account_id,)
                        if source is not None
                        else ()
                    ),
                )
            )
            event = (
                self.session.get(CanonicalEvent, binding.canonical_event_id)
                if binding is not None
                else None
            )
            return [event] if event is not None else []

        sender_event_id = correlation.get("sender_event_id")
        if isinstance(sender_event_id, str):
            observations = self.session.scalars(select(EventObservation)).all()
            event_ids = {
                observation.canonical_event_id
                for observation in observations
                if observation.canonical_event_id is not None
                and isinstance(observation.observed_fields.get("correlation"), dict)
                and observation.observed_fields["correlation"].get("sender_event_id")
                == sender_event_id
            }
            if event_ids:
                return list(
                    self.session.scalars(
                        select(CanonicalEvent).where(
                            CanonicalEvent.id.in_(event_ids),
                            CanonicalEvent.status.in_(correlatable_statuses),
                        )
                    )
                )

        structured = fields.get("event")
        title_hint = correlation.get("title_hint") or candidate.title
        candidates = list(
            self.session.scalars(
                select(CanonicalEvent).where(CanonicalEvent.status.in_(correlatable_statuses))
            )
        )
        title_matches = [
            event
            for event in candidates
            if _normalized_title(event.title) == _normalized_title(str(title_hint))
        ]
        if isinstance(structured, dict):
            fingerprint = _event_fingerprint(structured)
            exact = [
                event
                for event in title_matches
                if _event_fingerprint(event.event_spec) == fingerprint
            ]
            if exact:
                return exact
            if candidate.mutation == "create":
                # A generic title is not event identity. Two independent creates
                # named "General meeting" must remain distinct unless their full
                # material event fingerprint or a stronger provider/sender key
                # correlates them.
                return []
        date_hint = correlation.get("date_hint")
        if isinstance(date_hint, str):
            return [
                event
                for event in title_matches
                if date_hint in repr(event.event_spec.get("timing"))
            ]
        return title_matches

    def create_from_candidate(
        self,
        candidate: SemanticCandidate,
        *,
        event: StandaloneCalendarEventInput,
    ) -> CanonicalEvent:
        entity_refs = _candidate_entity_refs(candidate)
        context_labels = candidate.fields.get("context_labels")
        canonical = CanonicalEvent(
            canonical_key=f"semantic_candidate:{candidate.id}",
            title=event.title,
            status="proposed",
            event_spec=event.model_dump(mode="json"),
            reminder_plan=(
                event.reminder_plan.model_dump(mode="json")
                if event.reminder_plan is not None
                else None
            ),
            calendar_lane=event.calendar_lane,
            entity_refs=entity_refs,
            context_labels=(
                [str(value) for value in context_labels] if isinstance(context_labels, list) else []
            ),
            authority="inferred",
        )
        self.session.add(canonical)
        self.session.flush()
        return canonical

    def adopt_provider_event(
        self,
        candidate: SemanticCandidate,
        *,
        account_id: uuid.UUID,
        calendar_id: str,
        provider_event: CalendarEventCache,
        event: StandaloneCalendarEventInput | None,
    ) -> tuple[CanonicalEvent, ProviderEventBinding]:
        existing_binding = self.session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.account_id == account_id,
                ProviderEventBinding.calendar_id == calendar_id,
                ProviderEventBinding.provider_event_id == provider_event.provider_event_id,
            )
        )
        if existing_binding is not None:
            canonical = self.session.get(CanonicalEvent, existing_binding.canonical_event_id)
            if canonical is None:
                raise DocketError(
                    code="canonical_event_not_found",
                    message="The provider binding lost its canonical event.",
                )
            return canonical, existing_binding
        entity_refs = _candidate_entity_refs(candidate)
        snapshot = _cache_snapshot(provider_event)
        provider_lane = CalendarLaneService(self.session, get_settings()).require_active(
            account_id,
            calendar_id=calendar_id,
        ).lane
        if event is not None:
            event = event.model_copy(update={"calendar_lane": provider_lane})
        canonical = CanonicalEvent(
            canonical_key=(
                f"provider:{account_id}:{calendar_id}:{provider_event.provider_event_id}"
            ),
            title=event.title if event is not None else provider_event.summary or candidate.title,
            status="active",
            event_spec=(
                event.model_dump(mode="json")
                if event is not None
                else {"provider_snapshot": snapshot}
            ),
            reminder_plan=None,
            calendar_lane=provider_lane,
            entity_refs=entity_refs,
            context_labels=[
                str(value)
                for value in candidate.fields.get("context_labels", [])
                if isinstance(value, str)
            ],
            authority="canonical",
        )
        self.session.add(canonical)
        self.session.flush()
        binding = ProviderEventBinding(
            canonical_event_id=canonical.id,
            account_id=account_id,
            calendar_id=calendar_id,
            provider_event_id=provider_event.provider_event_id,
            provider_etag=provider_event.provider_etag,
            status="active",
            provider_snapshot=snapshot,
        )
        self.session.add(binding)
        self.session.flush()
        return canonical, binding

    def observe(
        self,
        *,
        candidate: SemanticCandidate,
        canonical_event: CanonicalEvent | None,
        correlation_state: str,
        candidate_events: list[CanonicalEvent],
    ) -> EventObservation:
        observation = EventObservation(
            canonical_event_id=canonical_event.id if canonical_event is not None else None,
            source_item_id=candidate.source_item_id,
            semantic_candidate_id=candidate.id,
            mutation=candidate.mutation,
            observed_fields=dict(candidate.fields),
            confidence=candidate.confidence,
            correlation_state=correlation_state,
            candidate_event_ids=[str(event.id) for event in candidate_events],
            observed_at=utc_now(),
        )
        self.session.add(observation)
        self.session.flush()
        return observation


class SemanticCandidateCompiler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    def _claim(self) -> uuid.UUID | None:
        now = utc_now()
        with self.session_factory.begin() as session:
            candidate = session.scalar(
                select(SemanticCandidate)
                .where(
                    SemanticCandidate.status == "pending",
                    or_(
                        SemanticCandidate.next_attempt_at.is_(None),
                        SemanticCandidate.next_attempt_at <= now,
                    ),
                )
                .order_by(SemanticCandidate.created_at, SemanticCandidate.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            candidate.status = "resolving"
            candidate.version += 1
            return candidate.id

    @staticmethod
    def _calendar_account(session: Session, source: SourceItem) -> Account:
        source_account = session.get(Account, source.account_id)
        if (
            source_account is not None
            and source_account.enabled
            and "google_calendar" in source_account.capabilities
        ):
            return source_account
        matches = list(
            session.scalars(
                select(Account).where(
                    Account.provider == "google",
                    Account.enabled.is_(True),
                )
            )
        )
        matches = [account for account in matches if "google_calendar" in account.capabilities]
        if len(matches) != 1:
            raise DocketError(
                code="calendar_account_unresolved",
                message="The inferred event does not resolve to one Calendar account.",
            )
        return matches[0]

    @staticmethod
    def _clarification(
        session: Session,
        candidate: SemanticCandidate,
        *,
        reason: str,
        details: dict[str, Any],
        settings: Settings,
    ) -> None:
        entity_gaps = details.get("entity_gaps")
        summary = candidate.summary
        if reason == "entity_resolution_required" and isinstance(entity_gaps, list):
            labels = [
                f"{gap.get('entity_class')} “{gap.get('mention')}”"
                for gap in entity_gaps
                if isinstance(gap, dict)
            ]
            if labels:
                summary = (
                    "Confirm or register "
                    + ", ".join(labels[:3])
                    + " before Docket continues this formulation."
                )
        queue_item = QueueItem(
            primary_source_item_id=candidate.source_item_id,
            deduplication_key=f"semantic_clarification:{candidate.id}",
            material_fingerprint=sha256_json(
                {"candidate_id": str(candidate.id), "reason": reason, "details": details}
            ),
            category="clarification",
            title=f"Clarification needed · {candidate.title}"[:512],
            summary=summary,
            status="pending",
            priority="normal",
            presentation="clarification",
            received_at=utc_now(),
        )
        session.add(queue_item)
        session.flush()
        session.add(
            QueueItemSource(
                queue_item_id=queue_item.id,
                source_item_id=candidate.source_item_id,
                relationship="primary",
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=f"discord_projection:{queue_item.id}:1",
                payload={
                    "queue_item_id": str(queue_item.id),
                    "target_local_date": queue_projection_date(queue_item, settings).isoformat(),
                },
                status="pending",
            )
        )
        candidate.status = "needs_clarification"
        candidate.queue_item_id = queue_item.id
        candidate.resolution = {"reason": reason, **details}

    @staticmethod
    def _attention_card(
        session: Session,
        candidate: SemanticCandidate,
        *,
        settings: Settings,
    ) -> None:
        queue_item = QueueItem(
            primary_source_item_id=candidate.source_item_id,
            deduplication_key=f"semantic_attention:{candidate.id}",
            material_fingerprint=sha256_json(
                {
                    "candidate_id": str(candidate.id),
                    "kind": candidate.kind,
                    "summary": candidate.summary,
                }
            ),
            category=candidate.kind,
            title=candidate.title,
            summary=candidate.summary,
            status="pending",
            priority="normal",
            presentation="action_required",
            received_at=utc_now(),
        )
        session.add(queue_item)
        session.flush()
        session.add(
            QueueItemSource(
                queue_item_id=queue_item.id,
                source_item_id=candidate.source_item_id,
                relationship="primary",
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=f"discord_projection:{queue_item.id}:1",
                payload={
                    "queue_item_id": str(queue_item.id),
                    "target_local_date": queue_projection_date(queue_item, settings).isoformat(),
                },
                status="pending",
            )
        )
        candidate.status = "resolved"
        candidate.queue_item_id = queue_item.id
        candidate.resolution = {"disposition": "action_required"}

    @staticmethod
    def _supersede_pending_formulation(
        session: Session,
        canonical_event: CanonicalEvent,
        *,
        reason: str,
    ) -> None:
        action_ids = set(
            session.scalars(
                select(ActionRevision.action_id).where(
                    ActionRevision.parameters["canonical_event_id"].as_string()
                    == str(canonical_event.id)
                )
            )
        )
        for action_id in action_ids:
            action = session.get(Action, action_id)
            if action is None or action.status != "approval_pending":
                continue
            revision = session.scalar(
                select(ActionRevision).where(
                    ActionRevision.action_id == action.id,
                    ActionRevision.revision == action.current_revision,
                )
            )
            if revision is None or revision.parameters.get("canonical_event_id") != str(
                canonical_event.id
            ):
                continue
            approval = session.scalar(
                select(Approval).where(
                    Approval.action_revision_id == revision.id,
                    Approval.status == "pending",
                )
            )
            control_projection = (
                session.get(DiscordProjection, approval.control_projection_id)
                if approval is not None and approval.control_projection_id is not None
                else None
            )
            if approval is not None:
                approval.status = "superseded"
                approval.responded_at = utc_now()
            action.status = "superseded"
            queue_item = (
                session.get(QueueItem, action.queue_item_id)
                if action.queue_item_id is not None
                else None
            )
            if queue_item is not None:
                queue_item.status = "completed"
                queue_item.resolved_at = utc_now()
                queue_item.resolution_code = reason
                queue_item.version += 1
                superseded_candidates = session.scalars(
                    select(SemanticCandidate).where(
                        SemanticCandidate.queue_item_id == queue_item.id,
                        SemanticCandidate.status.in_(
                            ("needs_clarification", "proposed", "executing")
                        ),
                    )
                ).all()
                for candidate in superseded_candidates:
                    candidate.status = "resolved"
                    candidate.resolution = {
                        **(candidate.resolution or {}),
                        "disposition": reason,
                        "canonical_event_id": str(canonical_event.id),
                    }
                    candidate.version += 1
                projection = control_projection or morning_brief_projection_for_queue_item(
                    session,
                    child_queue_item_id=queue_item.id,
                )
                refresh_target = projection_refresh_target(
                    session,
                    child_queue_item_id=queue_item.id,
                    projection=projection,
                )
                session.add(
                    OutboxEvent(
                        event_type="discord.projection.refresh_requested",
                        aggregate_type="queue_item",
                        aggregate_id=refresh_target.queue_item_id,
                        deduplication_key=(
                            f"discord_projection:{refresh_target.queue_item_id}:"
                            f"superseded:{revision.id}"
                        ),
                        payload={
                            **refresh_target.payload(),
                            "status": "superseded",
                        },
                        status="pending",
                    )
                )

    @staticmethod
    def _cache_matches_event(
        cached: CalendarEventCache,
        event: StandaloneCalendarEventInput,
    ) -> bool:
        if cached.status not in {"confirmed", "tentative"}:
            return False
        if cached.recurrence_kind != event.recurrence_kind:
            return False
        if _normalized_title(cached.summary or "") != _normalized_title(event.title):
            return False
        if event.location is not None and _normalized_title(
            cached.location or ""
        ) != _normalized_title(event.location):
            return False
        timing = event.model_dump(mode="json")["timing"]
        if timing["kind"] == "all_day":
            return (
                cached.is_all_day
                and cached.start_date is not None
                and cached.end_date is not None
                and cached.start_date.isoformat() == timing["start_date"]
                and cached.end_date.isoformat() == timing["end_date"]
            )
        if cached.is_all_day or cached.start_at is None or cached.end_at is None:
            return False
        intervals = _occurrence_intervals(event)
        return any(
            _as_utc(cached.start_at) == start and _as_utc(cached.end_at) == end
            for start, end in intervals
        )

    def _cached_provider_event(
        self,
        session: Session,
        *,
        account_id: uuid.UUID,
        event: StandaloneCalendarEventInput | None,
        provider_event_id: str | None,
        identity_only: bool,
    ) -> CalendarEventCache | None:
        calendar_ids = CalendarLaneService(session, self.settings).calendar_ids(account_id)
        statement = select(CalendarEventCache).where(
            CalendarEventCache.account_id == account_id,
            CalendarEventCache.calendar_id.in_(calendar_ids),
            CalendarEventCache.status.in_(("confirmed", "tentative")),
        )
        if provider_event_id is not None:
            statement = statement.where(
                or_(
                    CalendarEventCache.provider_event_id == provider_event_id,
                    CalendarEventCache.recurring_event_id == provider_event_id,
                )
            )
        elif event is None:
            return None
        rows = list(
            session.scalars(
                statement.order_by(
                    CalendarEventCache.start_at,
                    CalendarEventCache.start_date,
                    CalendarEventCache.provider_event_id,
                )
            )
        )
        if identity_only and provider_event_id is not None:
            return rows[0] if rows else None
        if event is None:
            return None
        matches = [row for row in rows if self._cache_matches_event(row, event)]
        return matches[0] if len(matches) == 1 else None

    def _inferred_lane(
        self,
        session: Session,
        candidate: SemanticCandidate,
        event_data: dict[str, Any],
    ) -> str:
        explicit = event_data.get("calendar_lane")
        if explicit in {"academic", "work", "organizations", "personal", "unsorted"}:
            return str(explicit)
        defaults: set[str] = set()
        class_hints: set[str] = set()
        for ref in _candidate_entity_refs(candidate):
            try:
                entity = session.get(Entity, uuid.UUID(str(ref["entity_id"])))
            except (KeyError, ValueError):
                continue
            if entity is None:
                continue
            configured = entity.attributes.get("calendar_lane_default")
            if configured in {
                "academic",
                "work",
                "organizations",
                "personal",
                "unsorted",
            }:
                defaults.add(str(configured))
            elif entity.entity_class in {"course", "institution"}:
                class_hints.add("academic")
            elif entity.entity_class == "organization":
                class_hints.add("organizations")
        choices = defaults or class_hints
        return next(iter(choices)) if len(choices) == 1 else "unsorted"

    def _compile_event(
        self,
        session: Session,
        candidate: SemanticCandidate,
        source: SourceItem,
    ) -> None:
        event_data = candidate.fields.get("event")
        missing_fields = candidate.fields.get("missing_fields")
        event = (
            StandaloneCalendarEventInput.model_validate(event_data)
            if isinstance(event_data, dict)
            else None
        )
        if candidate.mutation in {"create", "update"} and event is None:
            self._clarification(
                session,
                candidate,
                reason="required_event_fields_missing",
                details={
                    "missing_fields": (
                        list(missing_fields) if isinstance(missing_fields, list) else []
                    )
                },
                settings=self.settings,
            )
            return
        effective_mutation = candidate.mutation
        account = self._calendar_account(session, source)
        if event is not None and isinstance(event_data, dict):
            event = event.model_copy(
                update={"calendar_lane": self._inferred_lane(session, candidate, event_data)}
            )
        lane_service = CalendarLaneService(session, self.settings)
        active_calendar_ids = lane_service.calendar_ids(account.id)
        event_service = CanonicalEventService(session)
        matches = event_service.correlate(candidate)
        if len(matches) > 1:
            event_service.observe(
                candidate=candidate,
                canonical_event=None,
                correlation_state="ambiguous",
                candidate_events=matches,
            )
            self._clarification(
                session,
                candidate,
                reason="event_identity_ambiguous",
                details={"candidate_event_ids": [str(match.id) for match in matches]},
                settings=self.settings,
            )
            return

        canonical_event = matches[0] if matches else None
        if event is not None and canonical_event is not None:
            event = event.model_copy(update={"calendar_lane": canonical_event.calendar_lane})
        binding: ProviderEventBinding | None = None
        correlation = candidate.fields.get("correlation")
        correlation = correlation if isinstance(correlation, dict) else {}
        provider_event_id = correlation.get("provider_event_id")
        provider_event_id = provider_event_id if isinstance(provider_event_id, str) else None
        if canonical_event is not None:
            binding = session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.canonical_event_id == canonical_event.id,
                    ProviderEventBinding.account_id == account.id,
                    ProviderEventBinding.calendar_id.in_(active_calendar_ids),
                    ProviderEventBinding.status.in_(("active", "diverged")),
                )
            )
            if binding is None:
                executing_revision = session.scalar(
                    select(ActionRevision)
                    .join(Action, Action.id == ActionRevision.action_id)
                    .where(
                        ActionRevision.parameters["canonical_event_id"].as_string()
                        == str(canonical_event.id),
                        Action.status.in_(
                            (
                                "ready",
                                "executing",
                                "reconciliation_required",
                            )
                        ),
                    )
                    .limit(1)
                )
                if executing_revision is not None:
                    candidate.status = "pending"
                    candidate.next_attempt_at = utc_now() + timedelta(seconds=30)
                    candidate.resolution = {
                        "disposition": "awaiting_provider_binding",
                        "canonical_event_id": str(canonical_event.id),
                    }
                    return

        provider_match: CalendarEventCache | None = None
        if canonical_event is None:
            provider_match = self._cached_provider_event(
                session,
                account_id=account.id,
                event=event,
                provider_event_id=provider_event_id,
                identity_only=candidate.mutation in {"update", "cancel"},
            )
            if (
                provider_match is None
                and provider_event_id is not None
                and candidate.mutation == "create"
                and self._cached_provider_event(
                    session,
                    account_id=account.id,
                    event=None,
                    provider_event_id=provider_event_id,
                    identity_only=True,
                )
                is not None
            ):
                self._clarification(
                    session,
                    candidate,
                    reason="provider_event_details_mismatch",
                    details={},
                    settings=self.settings,
                )
                return
            if provider_match is not None:
                canonical_event, binding = event_service.adopt_provider_event(
                    candidate,
                    account_id=account.id,
                    calendar_id=provider_match.calendar_id,
                    provider_event=provider_match,
                    event=event if candidate.mutation in {"create", "none"} else None,
                )
                matches = [canonical_event]

        if candidate.mutation == "none":
            event_service.observe(
                candidate=candidate,
                canonical_event=canonical_event,
                correlation_state="matched" if canonical_event is not None else "unresolved",
                candidate_events=matches,
            )
            candidate.status = "resolved"
            candidate.resolution = {
                "disposition": (
                    "provider_confirmation_matched"
                    if provider_match is not None
                    else "canonical_confirmation_matched"
                    if canonical_event is not None
                    else "non_mutating_observation"
                ),
                "canonical_event_id": (
                    str(canonical_event.id) if canonical_event is not None else None
                ),
            }
            return

        if (
            candidate.mutation == "cancel"
            and canonical_event is not None
            and (canonical_event.status == "cancelled")
        ):
            event_service.observe(
                candidate=candidate,
                canonical_event=canonical_event,
                correlation_state="matched",
                candidate_events=matches,
            )
            candidate.status = "resolved"
            candidate.resolution = {
                "disposition": "canonical_already_cancelled",
                "canonical_event_id": str(canonical_event.id),
            }
            return

        if candidate.mutation == "create" and canonical_event is not None:
            assert event is not None
            if provider_match is not None or _event_fingerprint(
                canonical_event.event_spec
            ) == _event_fingerprint(event.model_dump(mode="json")):
                event_service.observe(
                    candidate=candidate,
                    canonical_event=canonical_event,
                    correlation_state="matched",
                    candidate_events=matches,
                )
                candidate.status = "resolved"
                candidate.resolution = {
                    "disposition": (
                        "calendar_already_matches"
                        if provider_match is not None
                        else "duplicate_observation"
                    ),
                    "canonical_event_id": str(canonical_event.id),
                }
                return
            effective_mutation = "update"

        if (
            effective_mutation == "update"
            and canonical_event is not None
            and event is not None
            and _event_fingerprint(canonical_event.event_spec)
            == _event_fingerprint(event.model_dump(mode="json"))
        ):
            provider_already_matches = binding is not None and (
                self._cached_provider_event(
                    session,
                    account_id=account.id,
                    event=event,
                    provider_event_id=binding.provider_event_id,
                    identity_only=False,
                )
                is not None
            )
            if binding is None or provider_already_matches:
                event_service.observe(
                    candidate=candidate,
                    canonical_event=canonical_event,
                    correlation_state="matched",
                    candidate_events=matches,
                )
                candidate.status = "resolved"
                candidate.resolution = {
                    "disposition": (
                        "calendar_already_matches"
                        if provider_already_matches
                        else "duplicate_observation"
                    ),
                    "canonical_event_id": str(canonical_event.id),
                }
                return

        if effective_mutation in {"update", "cancel"} and canonical_event is None:
            event_service.observe(
                candidate=candidate,
                canonical_event=None,
                correlation_state="unresolved",
                candidate_events=[],
            )
            self._clarification(
                session,
                candidate,
                reason="event_identity_unresolved",
                details={},
                settings=self.settings,
            )
            return

        if canonical_event is None:
            assert event is not None
            canonical_event = event_service.create_from_candidate(candidate, event=event)
            correlation_state = "new"
        else:
            correlation_state = "matched"
        event_service.observe(
            candidate=candidate,
            canonical_event=canonical_event,
            correlation_state=correlation_state,
            candidate_events=matches,
        )

        if effective_mutation in {"update", "cancel"} and binding is None:
            self._supersede_pending_formulation(
                session,
                canonical_event,
                reason="newer_evidence_superseded_proposal",
            )
            if effective_mutation == "cancel":
                canonical_event.status = "cancelled"
                canonical_event.version += 1
                candidate.status = "resolved"
                candidate.resolution = {
                    "disposition": "pending_create_cancelled",
                    "canonical_event_id": str(canonical_event.id),
                }
                return
            assert event is not None
            canonical_event.event_spec = event.model_dump(mode="json")
            canonical_event.title = event.title
            canonical_event.version += 1
            proposal: Any = CreateCalendarEventProposal(kind="create", event=event)
        elif effective_mutation == "update":
            assert binding is not None and event is not None
            proposal = UpdateCalendarEventProposal(
                kind="update",
                provider_event_id=binding.provider_event_id,
                replacement=event,
            )
        elif effective_mutation == "cancel":
            assert binding is not None
            proposal = CancelCalendarEventProposal(
                kind="cancel",
                provider_event_id=binding.provider_event_id,
                reason=candidate.summary,
            )
        else:
            assert event is not None
            proposal = CreateCalendarEventProposal(kind="create", event=event)

        result = CalendarActionService(session).formulate_inferred(
            InferredCalendarEventInput(
                account_id=account.id,
                calendar_id=(
                    binding.calendar_id
                    if binding is not None
                    else lane_service.require_active(
                        account.id,
                        lane_name=(
                            event.calendar_lane
                            if event is not None
                            else canonical_event.calendar_lane
                        ),
                    ).calendar_id
                ),
                proposal=proposal,
                canonical_event_id=canonical_event.id,
                semantic_candidate_id=candidate.id,
                source_item_id=source.id,
                entity_refs=_candidate_entity_refs(candidate),
                request_key=f"gmail:{source.id}:{candidate.id}",
            )
        )
        candidate.status = "proposed"
        candidate.queue_item_id = result.queue_item_id
        candidate.resolution = {
            "disposition": result.disposition,
            "canonical_event_id": str(canonical_event.id),
            "action_revision_id": str(result.action_revision_id),
        }

    def _compile(self, candidate_id: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            candidate = session.scalar(
                select(SemanticCandidate)
                .where(SemanticCandidate.id == candidate_id)
                .with_for_update()
            )
            if candidate is None or candidate.status != "resolving":
                return
            source = session.get(SourceItem, candidate.source_item_id)
            if source is None:
                candidate.status = "failed"
                candidate.resolution = {"error_code": "source_item_not_found"}
                return
            if candidate.kind == "noise":
                candidate.status = "suppressed"
                return
            if candidate.kind == "information":
                candidate.status = "resolved"
                candidate.resolution = {"disposition": "brief_eligible"}
                return
            if candidate.kind == "event":
                relevance = candidate.fields.get("calendar_relevance")
                if relevance == "excluded":
                    candidate.status = "suppressed"
                    candidate.resolution = {
                        "disposition": "preference_excluded",
                        "relevance_basis": candidate.fields.get("relevance_basis"),
                    }
                    return
                if relevance == "informational":
                    candidate.status = "resolved"
                    candidate.resolution = {
                        "disposition": "brief_eligible_event",
                        "relevance_basis": candidate.fields.get("relevance_basis"),
                    }
                    return
            raw_resolutions = candidate.fields.get("entity_resolutions")
            resolutions = raw_resolutions if isinstance(raw_resolutions, list) else []
            entity_gaps = [
                {
                    "mention": resolution.get("mention"),
                    "entity_class": resolution.get("entity_class"),
                    "role": resolution.get("role"),
                    "state": resolution.get("state"),
                    "resolution_id": resolution.get("resolution_id"),
                    "candidate_entity_ids": resolution.get("candidate_entity_ids", []),
                }
                for resolution in resolutions
                if isinstance(resolution, dict)
                and resolution.get("required", True)
                and resolution.get("state") in {"unresolved", "ambiguous"}
            ]
            if entity_gaps:
                self._clarification(
                    session,
                    candidate,
                    reason="entity_resolution_required",
                    details={"entity_gaps": entity_gaps},
                    settings=self.settings,
                )
                return
            if candidate.kind == "event":
                self._compile_event(session, candidate, source)
                return
            self._attention_card(session, candidate, settings=self.settings)

    def _retry(self, candidate_id: uuid.UUID, error: Exception) -> None:
        with self.session_factory.begin() as session:
            candidate = session.get(SemanticCandidate, candidate_id)
            if candidate is None or candidate.status != "resolving":
                return
            candidate.failure_count += 1
            code = error.code if isinstance(error, DocketError) else "candidate_compile_failed"
            if (
                code
                in {
                    "calendar_freshness_required",
                    "calendar_account_unresolved",
                }
                and candidate.failure_count < 10
            ):
                candidate.status = "pending"
                candidate.next_attempt_at = utc_now() + timedelta(
                    seconds=min(300, 2**candidate.failure_count)
                )
            else:
                candidate.status = "failed"
            candidate.resolution = {"error_code": code}

    def run_due_once(self) -> bool:
        candidate_id = self._claim()
        if candidate_id is None:
            return False
        try:
            self._compile(candidate_id)
        except Exception as exc:
            self._retry(candidate_id, exc)
        return True

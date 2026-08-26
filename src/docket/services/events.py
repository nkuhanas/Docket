from __future__ import annotations

import re
import uuid
from datetime import timedelta
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
    CanonicalEvent,
    EventObservation,
    OutboxEvent,
    ProviderEventBinding,
    QueueItem,
    QueueItemSource,
    SemanticCandidate,
    SourceItem,
    TriageWindow,
    TriageWindowMembership,
)
from docket.models.base import utc_now
from docket.schemas.actions import (
    CancelCalendarEventProposal,
    CreateCalendarEventProposal,
    InferredCalendarEventInput,
    UpdateCalendarEventProposal,
)
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.services.calendar_actions import CalendarActionService
from docket.services.queue import queue_projection_date

_SPACE = re.compile(r"\s+")


def _normalized_title(value: str) -> str:
    return _SPACE.sub(" ", value.casefold().strip())


def _event_fingerprint(event: dict[str, Any]) -> str:
    return sha256_json(
        {
            "title": _normalized_title(str(event.get("title") or "")),
            "timing": event.get("timing"),
            "recurrence": event.get("recurrence"),
            "location": event.get("location"),
        }
    )


class CanonicalEventService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def correlate(self, candidate: SemanticCandidate) -> list[CanonicalEvent]:
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
                            CanonicalEvent.status.in_(("proposed", "active")),
                        )
                    )
                )

        structured = fields.get("event")
        title_hint = correlation.get("title_hint") or candidate.title
        candidates = list(
            self.session.scalars(
                select(CanonicalEvent).where(CanonicalEvent.status.in_(("proposed", "active")))
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
        entity_resolutions = candidate.fields.get("entity_resolutions")
        entity_refs = (
            [dict(value) for value in entity_resolutions if isinstance(value, dict)]
            if isinstance(entity_resolutions, list)
            else []
        )
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
            entity_refs=entity_refs,
            context_labels=(
                [str(value) for value in context_labels] if isinstance(context_labels, list) else []
            ),
            authority="inferred",
        )
        self.session.add(canonical)
        self.session.flush()
        return canonical

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
        window = session.scalar(
            select(TriageWindow)
            .join(
                TriageWindowMembership,
                TriageWindowMembership.window_id == TriageWindow.id,
            )
            .where(TriageWindowMembership.semantic_candidate_id == candidate.id)
        )
        queue_item = QueueItem(
            primary_source_item_id=candidate.source_item_id,
            deduplication_key=f"semantic_clarification:{candidate.id}",
            material_fingerprint=sha256_json(
                {"candidate_id": str(candidate.id), "reason": reason, "details": details}
            ),
            category="clarification",
            title=f"Clarification needed · {candidate.title}"[:512],
            summary=candidate.summary,
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
        if window is None or window.window_kind == "waking":
            session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=f"discord_projection:{queue_item.id}:1",
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "target_local_date": queue_projection_date(
                            queue_item, settings
                        ).isoformat(),
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
        window = session.scalar(
            select(TriageWindow)
            .join(
                TriageWindowMembership,
                TriageWindowMembership.window_id == TriageWindow.id,
            )
            .where(TriageWindowMembership.semantic_candidate_id == candidate.id)
        )
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
        if window is None or window.window_kind == "waking":
            session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=f"discord_projection:{queue_item.id}:1",
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "target_local_date": queue_projection_date(
                            queue_item, settings
                        ).isoformat(),
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
        revisions = session.scalars(
            select(ActionRevision).where(
                ActionRevision.parameters["canonical_event_id"].as_string()
                == str(canonical_event.id)
            )
        ).all()
        for revision in revisions:
            action = session.get(Action, revision.action_id)
            if action is None or action.status != "approval_pending":
                continue
            approval = session.scalar(
                select(Approval).where(
                    Approval.action_revision_id == revision.id,
                    Approval.status == "pending",
                )
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
                session.add(
                    OutboxEvent(
                        event_type="discord.projection.refresh_requested",
                        aggregate_type="queue_item",
                        aggregate_id=queue_item.id,
                        deduplication_key=(
                            f"discord_projection:{queue_item.id}:superseded:{revision.id}"
                        ),
                        payload={"queue_item_id": str(queue_item.id), "status": "superseded"},
                        status="pending",
                    )
                )

    def _compile_event(
        self,
        session: Session,
        candidate: SemanticCandidate,
        source: SourceItem,
    ) -> None:
        event_data = candidate.fields.get("event")
        missing_fields = candidate.fields.get("missing_fields")
        if not isinstance(event_data, dict):
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
        event = StandaloneCalendarEventInput.model_validate(event_data)
        effective_mutation = candidate.mutation
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
        binding: ProviderEventBinding | None = None
        if canonical_event is not None:
            binding = session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.canonical_event_id == canonical_event.id,
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

        if candidate.mutation == "create" and canonical_event is not None:
            if _event_fingerprint(canonical_event.event_spec) == _event_fingerprint(event_data):
                event_service.observe(
                    candidate=candidate,
                    canonical_event=canonical_event,
                    correlation_state="matched",
                    candidate_events=matches,
                )
                candidate.status = "resolved"
                candidate.resolution = {
                    "disposition": "duplicate_observation",
                    "canonical_event_id": str(canonical_event.id),
                }
                return
            effective_mutation = "update"

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
            canonical_event.event_spec = event.model_dump(mode="json")
            canonical_event.title = event.title
            canonical_event.version += 1
            proposal: Any = CreateCalendarEventProposal(kind="create", event=event)
        elif effective_mutation == "update":
            assert binding is not None
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
            proposal = CreateCalendarEventProposal(kind="create", event=event)

        account = self._calendar_account(session, source)
        window = session.scalar(
            select(TriageWindow)
            .join(
                TriageWindowMembership,
                TriageWindowMembership.window_id == TriageWindow.id,
            )
            .where(TriageWindowMembership.semantic_candidate_id == candidate.id)
        )
        result = CalendarActionService(session).formulate_inferred(
            InferredCalendarEventInput(
                account_id=account.id,
                calendar_id=self.settings.google_calendar_id,
                proposal=proposal,
                canonical_event_id=canonical_event.id,
                semantic_candidate_id=candidate.id,
                source_item_id=source.id,
                request_key=f"gmail:{source.id}:{candidate.id}",
                defer_projection=window is not None and window.window_kind == "overnight",
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

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    DailyBrief,
    DailyBriefItem,
    OutboxEvent,
    QueueItem,
    SemanticCandidate,
    SourceItem,
    TriageWindow,
    TriageWindowMembership,
)
from docket.models.base import utc_now


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DailyBriefService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.zone = ZoneInfo(self.settings.timezone)

    def _window_bounds(self, kind: str, local_date: date) -> tuple[datetime, datetime]:
        start_hour = self.settings.waking_window_start_hour
        end_hour = self.settings.waking_window_end_hour
        if kind == "waking":
            start_local = datetime.combine(local_date, time(start_hour), tzinfo=self.zone)
            end_local = datetime.combine(local_date, time(end_hour), tzinfo=self.zone)
            if end_local <= start_local:
                end_local += timedelta(days=1)
        else:
            end_local = datetime.combine(local_date, time(start_hour), tzinfo=self.zone)
            start_date = local_date if start_hour > end_hour else local_date - timedelta(days=1)
            start_local = datetime.combine(start_date, time(end_hour), tzinfo=self.zone)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def _window(
        self,
        session: Session,
        *,
        kind: str,
        local_date: date,
    ) -> TriageWindow:
        window = session.scalar(
            select(TriageWindow).where(
                TriageWindow.window_kind == kind,
                TriageWindow.local_date == local_date,
            )
        )
        if window is None:
            starts_at, ends_at = self._window_bounds(kind, local_date)
            window = TriageWindow(
                window_kind=kind,
                local_date=local_date,
                timezone=self.settings.timezone,
                starts_at=starts_at,
                ends_at=ends_at,
                status="open",
            )
            session.add(window)
            session.flush()
        elif window.status == "open":
            starts_at, ends_at = self._window_bounds(kind, local_date)
            if (
                window.timezone != self.settings.timezone
                or _aware(window.starts_at) != starts_at
                or _aware(window.ends_at) != ends_at
            ):
                window.timezone = self.settings.timezone
                window.starts_at = starts_at
                window.ends_at = ends_at
                window.version += 1
        return window

    def _reconcile_open_window_bounds(self, session: Session) -> None:
        windows = session.scalars(
            select(TriageWindow).where(TriageWindow.status == "open")
        ).all()
        for window in windows:
            starts_at, ends_at = self._window_bounds(window.window_kind, window.local_date)
            if (
                window.timezone == self.settings.timezone
                and _aware(window.starts_at) == starts_at
                and _aware(window.ends_at) == ends_at
            ):
                continue
            window.timezone = self.settings.timezone
            window.starts_at = starts_at
            window.ends_at = ends_at
            window.version += 1

    def assign_candidate(
        self,
        session: Session,
        candidate: SemanticCandidate,
        *,
        observed_at: datetime,
    ) -> TriageWindowMembership:
        local = _aware(observed_at).astimezone(self.zone)
        hour = local.hour
        start = self.settings.waking_window_start_hour
        end = self.settings.waking_window_end_hour
        waking = start <= hour < end if start < end else hour >= start or hour < end
        if waking:
            kind = "waking"
            anchor_date = local.date()
        else:
            kind = "overnight"
            if start < end:
                anchor_date = local.date() if hour < start else local.date() + timedelta(days=1)
            else:
                anchor_date = local.date()
        window = self._window(session, kind=kind, local_date=anchor_date)
        delayed_reason: str | None = None
        crossed_boundaries = 0
        while window.status == "published":
            crossed_boundaries += 1
            if kind == "overnight":
                kind = "waking"
            else:
                kind = "overnight"
                anchor_date += timedelta(days=1)
            window = self._window(session, kind=kind, local_date=anchor_date)
        if crossed_boundaries == 1:
            delayed_reason = (
                "delayed_after_morning_brief"
                if kind == "waking"
                else "delayed_after_night_brief"
            )
        elif crossed_boundaries > 1:
            delayed_reason = "delayed_after_multiple_briefs"
        membership = TriageWindowMembership(
            window_id=window.id,
            semantic_candidate_id=candidate.id,
            disposition="suppress" if candidate.kind == "noise" else "include",
            reason=("noise" if candidate.kind == "noise" else delayed_reason),
        )
        session.add(membership)
        return membership

    @staticmethod
    def _section(candidate: SemanticCandidate) -> str:
        if candidate.kind == "event":
            return "Calendar"
        if candidate.kind in {"response", "deadline", "task"}:
            return "Action required"
        return "Awareness"

    @classmethod
    def _brief_item(
        cls,
        session: Session,
        kind: str,
        candidate: SemanticCandidate,
    ) -> tuple[str, str]:
        if kind == "morning":
            return cls._section(candidate), candidate.title
        queue_item = (
            session.get(QueueItem, candidate.queue_item_id)
            if candidate.queue_item_id is not None
            else None
        )
        if queue_item is not None and queue_item.status == "completed":
            suffix = {
                "approval_rejected": "rejected",
                "calendar_cancelled": "cancelled",
                "calendar_synchronized": "completed",
                "calendar_conflict_keep_existing": "kept existing",
                "calendar_conflict_keep_both": "kept both",
                "calendar_conflict_new_wins": "resolved",
            }.get(queue_item.resolution_code or "", "resolved")
            return "Completed / resolved", f"{candidate.title} — {suffix}"
        if queue_item is not None and queue_item.status in {
            "pending",
            "awaiting_approval",
            "executing",
            "failed",
            "reconciliation_required",
            "snoozed",
        }:
            return "Still needs you", candidate.title
        if candidate.kind in {"response", "deadline", "task", "event"} and candidate.status in {
            "needs_clarification",
            "proposed",
            "executing",
            "failed",
        }:
            return "Still needs you", candidate.title
        return "Awareness", candidate.title

    @classmethod
    def _summary(
        cls,
        session: Session,
        kind: str,
        candidates: list[SemanticCandidate],
    ) -> str:
        heading = "Overnight" if kind == "morning" else "Today"
        included = [candidate for candidate in candidates if candidate.kind != "noise"]
        topic_order: list[str] = []
        latest_by_topic: dict[str, SemanticCandidate] = {}
        seen_topics: set[str] = set()
        for candidate in included:
            resolution = candidate.resolution or {}
            canonical_event_id = resolution.get("canonical_event_id")
            explicit_topic_key = candidate.fields.get("topic_key")
            if canonical_event_id is not None:
                topic_key = f"event:{canonical_event_id}"
            elif isinstance(explicit_topic_key, str) and explicit_topic_key:
                topic_key = f"{candidate.kind}:topic:{explicit_topic_key.casefold()}"
            else:
                topic_key = f"{candidate.kind}:candidate:{candidate.semantic_key}"
            if topic_key not in seen_topics:
                seen_topics.add(topic_key)
                topic_order.append(topic_key)
            latest_by_topic[topic_key] = candidate
        included = [latest_by_topic[topic_key] for topic_key in topic_order]
        if not included:
            return f"{heading}: no material changes or open items."
        grouped: dict[str, list[SemanticCandidate]] = {}
        display: dict[uuid.UUID, str] = {}
        for candidate in included:
            section, label = cls._brief_item(session, kind, candidate)
            grouped.setdefault(section, []).append(candidate)
            display[candidate.id] = label
        lines: list[str] = []
        section_order = (
            ("Action required", "Calendar", "Awareness")
            if kind == "morning"
            else ("Completed / resolved", "Still needs you", "Awareness")
        )
        for section in section_order:
            items = grouped.get(section, [])
            if not items:
                continue
            lines.append(f"{section} ({len(items)})")
            lines.extend(f"• {display[item.id]}" for item in items[:8])
            if len(items) > 8:
                lines.append(f"• {len(items) - 8} more")
        return "\n".join(lines)

    def _publish(self, *, kind: str, local_date: date) -> bool:
        window_kind = "overnight" if kind == "morning" else "waking"
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(DailyBrief).where(
                    DailyBrief.brief_kind == kind,
                    DailyBrief.local_date == local_date,
                )
            )
            if existing is not None:
                return False
            window = self._window(session, kind=window_kind, local_date=local_date)
            unclassified_source = session.scalar(
                select(SourceItem.id)
                .where(
                    SourceItem.status.in_(("staged", "claimed", "failed")),
                    func.coalesce(SourceItem.received_at, SourceItem.created_at)
                    >= window.starts_at,
                    func.coalesce(SourceItem.received_at, SourceItem.created_at) < window.ends_at,
                )
                .limit(1)
            )
            if unclassified_source is not None:
                return False
            memberships = list(
                session.execute(
                    select(TriageWindowMembership, SemanticCandidate)
                    .join(
                        SemanticCandidate,
                        SemanticCandidate.id == TriageWindowMembership.semantic_candidate_id,
                    )
                    .where(
                        TriageWindowMembership.window_id == window.id,
                        TriageWindowMembership.disposition == "include",
                    )
                    .order_by(SemanticCandidate.created_at, SemanticCandidate.id)
                )
            )
            candidates = [candidate for _membership, candidate in memberships]
            if any(candidate.status in {"pending", "resolving"} for candidate in candidates):
                return False
            summary = self._summary(session, kind, candidates)
            brief = DailyBrief(
                brief_kind=kind,
                local_date=local_date,
                window_id=window.id,
                status="pending",
                content_sha256=sha256_json(
                    {
                        "kind": kind,
                        "local_date": local_date.isoformat(),
                        "candidates": [str(candidate.id) for candidate in candidates],
                        "summary": summary,
                    }
                ),
            )
            session.add(brief)
            session.flush()
            for index, candidate in enumerate(candidates):
                section, _label = self._brief_item(session, kind, candidate)
                session.add(
                    DailyBriefItem(
                        brief_id=brief.id,
                        semantic_candidate_id=candidate.id,
                        section=section,
                        display_order=index,
                    )
                )
            queue_item = QueueItem(
                deduplication_key=f"daily_brief:{kind}:{local_date.isoformat()}",
                material_fingerprint=brief.content_sha256,
                category=f"{kind}_brief",
                title=("Morning brief" if kind == "morning" else "Night brief"),
                summary=summary,
                status="completed",
                priority="normal",
                presentation="awareness",
                received_at=utc_now(),
                resolved_at=utc_now(),
                resolution_code=f"{kind}_brief_published",
            )
            session.add(queue_item)
            session.flush()
            brief.queue_item_id = queue_item.id
            brief.status = "published"
            brief.published_at = utc_now()
            window.status = "published"
            window.version += 1
            session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=f"discord_projection:{queue_item.id}:1",
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "target_local_date": local_date.isoformat(),
                    },
                    status="pending",
                )
            )
            return True

    def run_due_once(self, now: datetime | None = None) -> bool:
        instant = _aware(now or utc_now()).astimezone(self.zone)
        published = False
        start = self.settings.waking_window_start_hour
        end = self.settings.waking_window_end_hour
        with self.session_factory.begin() as session:
            self._reconcile_open_window_bounds(session)
            due_windows = list(
                session.execute(
                    select(TriageWindow.window_kind, TriageWindow.local_date)
                    .where(
                        TriageWindow.status != "published",
                        TriageWindow.ends_at <= instant.astimezone(UTC),
                    )
                    .order_by(TriageWindow.ends_at)
                )
            )
        for window_kind, local_date in due_windows:
            kind = "morning" if window_kind == "overnight" else "night"
            published = self._publish(kind=kind, local_date=local_date) or published
        if instant.hour >= start:
            published = self._publish(kind="morning", local_date=instant.date()) or published
        if start < end and instant.hour >= end:
            published = self._publish(kind="night", local_date=instant.date()) or published
        elif start > end and end <= instant.hour < start:
            published = (
                self._publish(kind="night", local_date=instant.date() - timedelta(days=1))
                or published
            )
        return published

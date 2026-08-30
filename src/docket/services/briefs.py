from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    BriefEntry,
    DailyBrief,
    DailyBriefCaseMembership,
    DailyBriefEntryMembership,
    GmailSource,
    OperatorProjection,
    OutboxEvent,
    ProjectionDelivery,
    TriageWindow,
    TriageWindowMembership,
)
from docket.models.base import utc_now


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DailyBriefService:
    """Publish one replyable projection for each completed triage interval."""

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
        elif kind == "overnight":
            end_local = datetime.combine(local_date, time(start_hour), tzinfo=self.zone)
            start_date = local_date if start_hour > end_hour else local_date - timedelta(days=1)
            start_local = datetime.combine(start_date, time(end_hour), tzinfo=self.zone)
        else:
            raise ValueError(f"unsupported triage window kind: {kind}")
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def _window(self, session: Session, *, kind: str, local_date: date) -> TriageWindow:
        window = session.scalar(
            select(TriageWindow).where(
                TriageWindow.window_kind == kind,
                TriageWindow.local_date == local_date,
            )
        )
        starts_at, ends_at = self._window_bounds(kind, local_date)
        if window is None:
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
        elif window.status == "open" and (
            window.timezone != self.settings.timezone
            or _aware(window.starts_at) != starts_at
            or _aware(window.ends_at) != ends_at
        ):
            window.timezone = self.settings.timezone
            window.starts_at = starts_at
            window.ends_at = ends_at
            window.version += 1
        return window

    @staticmethod
    def _case_section(kind: str, case: AttentionCase) -> str:
        if case.status == "open":
            return "Action required" if kind == "morning" else "Still needs you"
        if case.status == "suppressed":
            return "Suppressed"
        return "Resolved" if kind == "morning" else "Completed / resolved"

    @classmethod
    def _render(
        cls,
        *,
        kind: str,
        cases: list[AttentionCase],
        entries: list[BriefEntry],
    ) -> tuple[str, dict[str, object]]:
        heading = "Morning brief" if kind == "morning" else "Night brief"
        grouped: dict[str, list[str]] = {}
        for case in cases:
            grouped.setdefault(cls._case_section(kind, case), []).append(
                f"{case.title} · {case.ref_id}"
            )
        for entry in entries:
            section = "Suppressed" if entry.disposition == "suppress" else "Awareness"
            grouped.setdefault(section, []).append(entry.title)
        order = (
            ("Action required", "Awareness", "Resolved", "Suppressed")
            if kind == "morning"
            else ("Completed / resolved", "Still needs you", "Awareness", "Suppressed")
        )
        lines = [heading]
        structured: list[dict[str, object]] = []
        for section in order:
            values = grouped.get(section, [])
            if not values:
                continue
            lines.append(f"{section} ({len(values)})")
            lines.extend(f"• {value}" for value in values[:25])
            structured.append({"section": section, "items": values[:25]})
        if len(lines) == 1:
            lines.append("No triage activity in this interval.")
        visible_text = "\n".join(lines)
        return visible_text[:12000], {"title": heading, "sections": structured}

    def _publish(self, *, kind: str, local_date: date) -> bool:
        window_kind = "overnight" if kind == "morning" else "waking"
        with self.session_factory.begin() as session:
            if session.scalar(
                select(DailyBrief.id).where(
                    DailyBrief.brief_kind == kind,
                    DailyBrief.local_date == local_date,
                )
            ) is not None:
                return False
            window = self._window(session, kind=window_kind, local_date=local_date)
            if window.status == "published":
                return False
            unclassified_source = session.scalar(
                select(GmailSource.id)
                .where(
                    GmailSource.status.in_(("staged", "claimed", "failed")),
                    func.coalesce(GmailSource.received_at, GmailSource.created_at)
                    >= window.starts_at,
                    func.coalesce(GmailSource.received_at, GmailSource.created_at)
                    < window.ends_at,
                )
                .limit(1)
            )
            if unclassified_source is not None:
                return False
            cases = list(
                session.scalars(
                    select(AttentionCase)
                    .where(
                        or_(
                            (AttentionCase.first_observed_at >= window.starts_at)
                            & (AttentionCase.first_observed_at < window.ends_at),
                            (AttentionCase.last_observed_at >= window.starts_at)
                            & (AttentionCase.last_observed_at < window.ends_at),
                            (AttentionCase.resolved_at >= window.starts_at)
                            & (AttentionCase.resolved_at < window.ends_at),
                        )
                    )
                    .order_by(AttentionCase.first_observed_at, AttentionCase.ref_id)
                )
            )
            entries = list(
                session.scalars(
                    select(BriefEntry)
                    .where(
                        BriefEntry.created_at >= window.starts_at,
                        BriefEntry.created_at < window.ends_at,
                    )
                    .order_by(BriefEntry.created_at, BriefEntry.ref_id)
                )
            )
            revisions: list[tuple[AttentionCase, AttentionCaseRevision]] = []
            for case in cases:
                revision = session.scalar(
                    select(AttentionCaseRevision).where(
                        AttentionCaseRevision.attention_case_id == case.id,
                        AttentionCaseRevision.revision == case.latest_revision,
                    )
                )
                if revision is not None:
                    revisions.append((case, revision))
            visible_text, semantic_content = self._render(
                kind=kind, cases=cases, entries=entries
            )
            case_refs = [case.ref_id for case, _revision in revisions]
            basis_refs = list(
                dict.fromkeys(
                    [
                        *(revision.ref_id for _case, revision in revisions),
                        *(entry.ref_id for entry in entries),
                    ]
                )
            )
            content_hash = sha256_json(
                {
                    "kind": kind,
                    "local_date": local_date.isoformat(),
                    "window": [window.starts_at.isoformat(), window.ends_at.isoformat()],
                    "case_revisions": [revision.ref_id for _case, revision in revisions],
                    "brief_entries": [entry.ref_id for entry in entries],
                    "visible_text": visible_text,
                }
            )
            brief = DailyBrief(
                brief_kind=kind,
                local_date=local_date,
                window_id=window.id,
                status="pending",
                content_sha256=content_hash,
                interval_start=window.starts_at,
                interval_end=window.ends_at,
                case_refs=case_refs,
                basis_refs=basis_refs,
            )
            session.add(brief)
            session.flush()
            for index, entry in enumerate(entries):
                section = "Suppressed" if entry.disposition == "suppress" else "Awareness"
                session.add(
                    DailyBriefEntryMembership(
                        brief_id=brief.id,
                        brief_entry_id=entry.id,
                        section=section,
                        display_order=index,
                    )
                )
                if session.get(TriageWindowMembership, (window.id, entry.id)) is None:
                    session.add(
                        TriageWindowMembership(
                            window_id=window.id,
                            brief_entry_id=entry.id,
                            disposition=entry.disposition,
                            reason=entry.reason,
                        )
                    )
                entry.included_brief_ref = brief.ref_id
            for index, (case, revision) in enumerate(revisions):
                session.add(
                    DailyBriefCaseMembership(
                        brief_id=brief.id,
                        attention_case_id=case.id,
                        case_revision_ref=revision.ref_id,
                        section=self._case_section(kind, case),
                        display_order=index,
                    )
                )
            projection = OperatorProjection(
                projection_kind="daily_brief",
                operator_ref=f"discord_user:{self.settings.operator_discord_user_id}",
                primary_public_ref=brief.ref_id,
                brief_ref=brief.ref_id,
                semantic_content={
                    **semantic_content,
                    "brief_ref": brief.ref_id,
                    "case_refs": case_refs,
                    "interval_start": window.starts_at.isoformat(),
                    "interval_end": window.ends_at.isoformat(),
                },
                visible_text=visible_text,
                render_schema_version=1,
                render_sha256=content_hash,
                component_sha256=sha256_json({"components": []}),
                basis_refs=basis_refs,
            )
            session.add(projection)
            session.flush()
            session.add(
                ProjectionDelivery(
                    projection_id=projection.id,
                    projection_ref=projection.ref_id,
                    transport="discord",
                    destination_ref=(
                        f"discord_conversation:{self.settings.discord_guild_id}:"
                        f"{self.settings.queue_channel_id}"
                    ),
                    status="pending",
                )
            )
            session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="operator_projection",
                    aggregate_id=projection.id,
                    deduplication_key=f"discord_projection:{projection.ref_id}",
                    payload={"projection_ref": projection.ref_id},
                    status="pending",
                )
            )
            brief.projection_ref = projection.ref_id
            brief.status = "published"
            brief.published_at = utc_now()
            window.status = "published"
            window.version += 1
            return True

    def run_due_once(self, now: datetime | None = None) -> bool:
        instant = _aware(now or utc_now()).astimezone(self.zone)
        start = self.settings.waking_window_start_hour
        end = self.settings.waking_window_end_hour
        published = False
        with self.session_factory() as session:
            due = list(
                session.execute(
                    select(TriageWindow.window_kind, TriageWindow.local_date)
                    .where(
                        TriageWindow.status != "published",
                        TriageWindow.ends_at <= instant.astimezone(UTC),
                    )
                    .order_by(TriageWindow.ends_at)
                )
            )
        for window_kind, local_date in due:
            published = self._publish(
                kind="morning" if window_kind == "overnight" else "night",
                local_date=local_date,
            ) or published
        if instant.hour >= start:
            published = self._publish(kind="morning", local_date=instant.date()) or published
        if start < end and instant.hour >= end:
            published = self._publish(kind="night", local_date=instant.date()) or published
        elif start > end and end <= instant.hour < start:
            published = self._publish(
                kind="night", local_date=instant.date() - timedelta(days=1)
            ) or published
        return published

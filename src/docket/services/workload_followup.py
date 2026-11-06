"""Bounded nonbinding operational review under existing canonical write authority."""

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.work_time import work_instant, work_zone
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    AuditEvent,
    CaseItem,
    ChangeSet,
    Engagement,
    Expense,
    FinancialDocument,
    NativeContext,
    OperationalReminder,
    Payment,
    PluginNotification,
    ReminderPlan,
    WorkCalendarProjection,
    WorkloadReconciliation,
    WorkSegment,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    OperationalReminderCreate,
    WorkloadReconcile,
    mutation_input_json,
)
from docket.schemas.followup import (
    OperationalReminderSpec,
    ReconcileWorkload,
    ScheduleFollowup,
    WorkloadException,
    WorkloadReconciliationSpec,
)
from docket.schemas.workload import ExceptionKind
from docket.services.business_commands import mutation_plan
from docket.services.compensation_policies import date_boundary, instant
from docket.services.financial_documents import FinancialDocumentService
from docket.services.financial_state import FinancialState
from docket.services.native_commands import NativeCommandService
from docket.services.notifications import NotificationService, identity_lock
from docket.services.provenance_refs import ProvenanceRefService
from docket.services.workload_calendar import WorkloadCalendarService
from docket.services.workload_context import WorkloadContextService

FOLLOWUP_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_schedule_followup": ScheduleFollowup,
    "docket_reconcile_workload": ReconcileWorkload,
}


def operational_subject(session: Session, ref: str, owner: str | None = None) -> Any:
    models: dict[str, Any] = {
        "work": WorkSession,
        "eng": Engagement,
        "fdoc": FinancialDocument,
        "exp": Expense,
        "pay": Payment,
    }
    model = models[ref.split("_", 1)[0]]
    row = session.scalar(select(model).where(model.ref_id == ref))
    if row is None or row.operator_ref != (owner or require_principal("read").operator_ref):
        raise DocketError(code="followup_subject_unavailable", message="Select an owned subject.")
    return row


class WorkloadFollowupService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def capture(self, public: ReconcileWorkload, as_of: datetime) -> WorkloadReconciliationSpec:
        workload = WorkloadContextService(self.session, self.settings)
        rules, preferences, flags = workload.preferences(None)
        zone = public.timezone or rules.timezone or self.settings.timezone
        today = as_of.astimezone(work_zone(zone)).date()
        first_day, last_day = (
            public.date_from or today - timedelta(days=7),
            public.date_to or today + timedelta(days=7),
        )
        if not 0 < (last_day - first_day).days <= 366:
            raise DocketError(
                code="invalid_workload_period", message="Use a positive period of at most 366 days."
            )
        first, last = (
            date_boundary(first_day, zone, "date_from"),
            date_boundary(last_day, zone, "date_to"),
        )
        found: dict[str, WorkloadException] = {}

        def add(
            category: ExceptionKind,
            ref: str,
            version: int,
            summary: str,
            *,
            related: dict[str, int] | None = None,
            detail: dict[str, Any] | None = None,
            identity: str = "",
        ) -> None:
            key = sha256_json([category, ref, sorted((related or {}).keys()), identity])
            found[key] = WorkloadException(
                key=key,
                category=category,
                subject_ref=ref,
                related_refs=list(related or {}),
                basis_versions={ref: version, **(related or {})},
                summary=summary,
                detail=detail or {},
            )

        current = list(
            self.session.execute(
                select(WorkSession, WorkSessionRevision)
                .join(
                    WorkSessionRevision,
                    (WorkSession.ref_id == WorkSessionRevision.work_ref)
                    & (WorkSession.version == WorkSessionRevision.revision),
                )
                .where(
                    WorkSession.operator_ref == require_principal("read").operator_ref,
                    WorkSession.lifecycle != "retracted",
                    WorkSession.superseded.is_(False),
                    or_(
                        WorkSession.lifecycle.in_(["running", "paused"]),
                        (WorkSessionRevision.start_at < last)
                        & (WorkSessionRevision.end_at > first),
                        (WorkSessionRevision.work_date >= first_day)
                        & (WorkSessionRevision.work_date < last_day),
                        (WorkSessionRevision.period_start < last_day)
                        & (WorkSessionRevision.period_end > first_day),
                    ),
                )
                .order_by(WorkSession.ref_id)
                .limit(1001)
            )
        )
        if len(current) > 1000:
            flags.append("work_scan_incomplete")
        current = current[:1000]
        by_revision = {revision.id: (head, revision) for head, revision in current}
        segments = list(
            self.session.scalars(
                select(WorkSegment)
                .where(
                    WorkSegment.revision_id.in_(by_revision),
                    WorkSegment.kind == "active",
                    WorkSegment.end_at.is_not(None),
                    WorkSegment.start_at < last,
                    WorkSegment.end_at > first,
                )
                .order_by(WorkSegment.start_at, WorkSegment.id)
                .limit(5001)
            )
        )
        if len(segments) > 5000:
            flags.append("work_segment_scan_incomplete")
        active: list[WorkSegment] = []
        for segment in segments[:5000]:
            start = instant(segment.start_at)
            active = [
                other
                for other in active
                if other.end_at is not None and instant(other.end_at) > start
            ]
            head, _ = by_revision[segment.revision_id]
            for other in active:
                previous, _ = by_revision[other.revision_id]
                if previous.ref_id != head.ref_id:
                    refs = sorted([previous.ref_id, head.ref_id])
                    versions = {previous.ref_id: previous.version, head.ref_id: head.version}
                    add(
                        "work_overlap",
                        refs[0],
                        versions[refs[0]],
                        "Reported work intervals overlap; review possible double-counting.",
                        related={refs[1]: versions[refs[1]]},
                    )
            active.append(segment)
        for head, revision in current:
            if (
                head.lifecycle in {"running", "paused"}
                and revision.start_at
                and as_of - instant(revision.start_at) >= timedelta(hours=12)
            ):
                add(
                    "open_timer",
                    head.ref_id,
                    head.version,
                    "A primary timer has remained open for at least 12 hours. "
                    "Review its reported end; no end was inferred.",
                    detail={
                        "heuristic_hours": 12,
                        "started_at": instant(revision.start_at).isoformat(),
                    },
                )
            if revision.engagement_ref and revision.reported_compensation_class in {
                None,
                "unknown",
            }:
                add(
                    "unclassified_work",
                    head.ref_id,
                    head.version,
                    "Work has an engagement but no reported compensation classification. "
                    "Classification remains optional; review only if useful.",
                )
        for row in workload.deadlines(last, as_of, zone):
            if row["overdue"]:
                add(
                    "overdue_task",
                    row["ref"],
                    row["version"],
                    "A reported due-time obligation is overdue.",
                    related={row["time_ref"]: row["time_version"]},
                )
        calendar = WorkloadCalendarService(self.session, self.settings).capture(first, last, as_of)
        flags.extend(sorted(calendar.flags))
        commitments = [
            r for r in calendar.rows if r.ref and r.version and not r.all_day and r.end > as_of
        ]
        if len(commitments) > 1000:
            flags.append("commitment_conflict_scan_incomplete")
        for i, commitment in enumerate(commitments[:1000]):
            assert commitment.ref is not None and commitment.version is not None
            for following in commitments[i + 1 : 1000]:
                if following.start >= commitment.end:
                    break
                if following.ref != commitment.ref and max(
                    commitment.start, following.start, as_of
                ) < min(commitment.end, following.end):
                    assert following.ref is not None and following.version is not None
                    refs = sorted([commitment.ref, following.ref])
                    versions = {
                        commitment.ref: commitment.version,
                        following.ref: following.version,
                    }
                    add(
                        "capacity_conflict",
                        refs[0],
                        versions[refs[0]],
                        "Known commitments overlap. Review reported firmness and travel; "
                        "no schedule change was made.",
                        related={refs[1]: versions[refs[1]]},
                        identity=min(commitment.start, following.start).isoformat(),
                    )
        state = FinancialState(self.session, as_of=as_of)
        for ref, target in state.sources.targets.items():
            if (
                target.get("due_on")
                and target["due_on"]
                < as_of.astimezone(work_zone(target.get("timezone") or zone)).date().isoformat()
                and any(state.match.outstanding[p.ref] > 0 for p in state.target_parts(ref))
            ):
                add(
                    "outstanding_money",
                    ref,
                    target["version"],
                    "A posted obligation has an outstanding balance past its reported due date.",
                    detail={"due_on": target["due_on"]},
                )
            if ref.startswith("fdoc_"):
                documents = FinancialDocumentService(self.session)
                document = documents.head(ref)
                revision = documents.revision(document)
                if document.kind == "invoice":
                    from docket.services.invoices import InvoiceService

                    reconciliation_flags = InvoiceService(self.session).flags(revision)
                else:
                    reconciliation_flags = documents.reconcile_flags(revision)
                if reconciliation_flags:
                    add(
                        "reconciliation",
                        ref,
                        document.version,
                        "A retained financial document needs reconciliation "
                        "with corrected source records.",
                    )
        # Explicit operational reminder instants supply reporting/follow-up deadlines
        # without inventing a Calendar event or an employer cutoff policy.
        reminders = self.session.execute(
            select(ReminderPlan, OperationalReminder)
            .join(OperationalReminder, OperationalReminder.reminder_ref == ReminderPlan.ref_id)
            .where(
                OperationalReminder.operator_ref == require_principal("read").operator_ref,
                ReminderPlan.canonical_status == "active",
                OperationalReminder.trigger_at >= as_of,
                OperationalReminder.trigger_at <= as_of + timedelta(days=3),
            )
            .limit(101)
        )
        for reminder, operational in reminders:
            subject = operational_subject(self.session, reminder.subject_ref)
            add(
                "reporting_cutoff",
                subject.ref_id,
                subject.version,
                "An explicitly scheduled operational follow-up is approaching.",
                related={reminder.ref_id: reminder.version},
                detail={"trigger_at": instant(operational.trigger_at).isoformat()},
            )
        stale_displays = list(
            self.session.execute(
                select(WorkCalendarProjection, WorkSession)
                .join(WorkSession, WorkSession.ref_id == WorkCalendarProjection.work_ref)
                .where(
                    WorkCalendarProjection.operator_ref == require_principal("read").operator_ref,
                    WorkCalendarProjection.enabled.is_(True),
                    WorkCalendarProjection.needs_refresh.is_(True),
                )
                .order_by(WorkCalendarProjection.ref_id)
                .limit(101)
            )
        )
        if len(stale_displays) > 100:
            flags.append("work_calendar_review_scan_incomplete")
        for display, displayed_work in stale_displays[:100]:
            add(
                "reconciliation",
                displayed_work.ref_id,
                displayed_work.version,
                "A Calendar work snapshot is pinned to an earlier report. Refresh or hide it; "
                "the actual work correction is already retained.",
                related={display.ref_id: display.version},
                identity="calendar_visibility",
            )
        ordered = sorted(found.values(), key=lambda x: (x.category, x.key))
        if len(ordered) > public.limit:
            flags.append("exception_limit_reached_narrow_period_or_review_again")
        flags += [
            "Reported facts only; missing work never proves inactivity.",
            "No external submission, performed-work or payment inference.",
        ]
        return WorkloadReconciliationSpec(
            as_of=as_of,
            date_from=first_day,
            date_to=last_day,
            timezone=zone,
            exceptions=ordered[: public.limit],
            coverage=flags[:25],
            preference_refs=[r["ref"] for r in preferences[:100]],
            notifications_enabled=rules.notifications_enabled is not False,
            suppressed_exceptions=rules.suppressed_exceptions or [],
            cooldown_hours=rules.notification_cooldown_hours or 168,
        )

    def apply(self, _session: Session, changeset: ChangeSet, change: Any) -> list[str]:
        spec = WorkloadReconciliationSpec.model_validate(change.create_spec)
        owner = require_principal("commit").operator_ref
        identity_lock(self.session, "workload-reconcile:" + owner)
        # Exact previews retain their source versions. New unrelated observations
        # do not change the retained proposal or fabricate current completeness.
        provenance = ProvenanceRefService(self.session)
        for exception in spec.exceptions:
            for ref, version in exception.basis_versions.items():
                row = provenance.get(ref)
                if getattr(row, "version", None) != version:
                    raise DocketError(
                        code="workload_basis_changed",
                        message="Refresh workload review after a source correction.",
                        details={"ref": ref},
                    )
        current_rules, _, _ = WorkloadContextService(self.session, self.settings).preferences(None)
        notifications_enabled = (
            spec.notifications_enabled and current_rules.notifications_enabled is not False
        )
        suppressed = set(spec.suppressed_exceptions) | set(
            current_rules.suppressed_exceptions or []
        )
        cooldown = max(spec.cooldown_hours, current_rules.notification_cooldown_hours or 168)
        cases = []
        for exception in spec.exceptions:
            situation = sha256_json(["workload", owner, exception.key])
            case = self.session.scalar(
                select(AttentionCase)
                .where(AttentionCase.situation_key == situation)
                .with_for_update()
            )
            payload = exception.model_dump(mode="json")
            fingerprint = sha256_json(payload)
            revision = (
                self.session.scalar(
                    select(AttentionCaseRevision).where(
                        AttentionCaseRevision.case_ref == case.ref_id,
                        AttentionCaseRevision.revision == case.latest_revision,
                    )
                )
                if case
                else None
            )
            changed = revision is None or revision.content_hash != fingerprint
            if case is None:
                case = AttentionCase(
                    situation_key=situation,
                    title="Workload: " + exception.category.replace("_", " "),
                    summary=exception.summary,
                    status="open",
                    priority="normal",
                    semantic_classes=["operational_review", exception.category],
                    first_observed_at=spec.as_of,
                    last_observed_at=spec.as_of,
                )
                self.session.add(case)
                self.session.flush()
            elif changed and case.status != "suppressed":
                case.status, case.resolution_decision_ref, case.resolved_at = "open", None, None
            case.last_observed_at = max(instant(case.last_observed_at), spec.as_of)
            if changed:
                if revision:
                    case.latest_revision += 1
                    case.version += 1
                case.summary = exception.summary
                item = CaseItem(
                    attention_case_id=case.id,
                    item_key="review:" + str(case.latest_revision),
                    item_type="decision_required",
                    resolution_role="supporting",
                    status="open",
                    payload_json=payload,
                    candidate_refs=[exception.subject_ref],
                    basis_refs=list(change.basis_refs),
                    source_refs=[],
                )
                self.session.add(item)
                self.session.flush()
                self.session.add(
                    AttentionCaseRevision(
                        attention_case_id=case.id,
                        case_ref=case.ref_id,
                        revision=case.latest_revision,
                        title=case.title,
                        summary=case.summary,
                        semantic_classes=list(case.semantic_classes),
                        case_item_refs=[item.ref_id],
                        source_refs=[],
                        admission_rule_ref="workload_exception_v1",
                        admission_basis_refs=list(change.basis_refs),
                        required_case_item_refs=[],
                        canonical_consequence_classes=[],
                        dependency_summary={
                            "subject_ref": exception.subject_ref,
                            "related_refs": exception.related_refs,
                            "as_of": spec.as_of.isoformat(),
                            "coverage": spec.coverage,
                        },
                        content_hash=fingerprint,
                    )
                )
            cases.append(case.ref_id)
            if (
                case.status == "open"
                and notifications_enabled
                and exception.category not in suppressed
            ):
                event = (
                    "docket.finance.attention"
                    if exception.category in {"outstanding_money", "reconciliation"}
                    and exception.subject_ref.startswith(("fdoc_", "exp_", "pay_"))
                    else "docket.work.attention"
                )
                latest = self.session.scalar(
                    select(PluginNotification)
                    .where(
                        PluginNotification.operator_ref == owner,
                        PluginNotification.subject_ref == case.ref_id,
                        PluginNotification.event_name == event,
                    )
                    .order_by(PluginNotification.created_at.desc())
                    .limit(1)
                )
                if (
                    latest is None
                    or instant(latest.created_at) + timedelta(hours=cooldown) <= spec.as_of
                ):
                    NotificationService(self.session, self.settings).publish(
                        event,
                        case.ref_id,
                        key="workload:"
                        + sha256_json([situation, fingerprint, spec.as_of.isoformat()]),
                    )
        record = WorkloadReconciliation(
            operator_ref=owner,
            snapshot=spec.model_dump(mode="json"),
            case_refs=cases,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(record)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="workload.reconciled",
                entity_type="workload_reconciliation",
                entity_id=record.id,
                primary_ref=record.ref_id,
                actor_type="agent",
                basis_refs=list(change.basis_refs),
                affected_refs=cases,
                data={"count": len(cases)},
            )
        )
        return [record.ref_id, *cases]


class WorkloadFollowupCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = FOLLOWUP_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)

        def build(context: NativeContext) -> dict[str, Any]:
            mutation: BaseModel
            if isinstance(public, ScheduleFollowup):
                operational_subject(self.session, public.subject_ref)
                zone = public.timezone or self.settings.timezone
                try:
                    point = datetime.fromisoformat(public.at)
                    if point.tzinfo is None:
                        raise ValueError("offset required")
                except ValueError as exc:
                    raise DocketError(
                        code="invalid_followup_time",
                        message="Supply an ISO timestamp with a UTC offset.",
                        details={"field": "at"},
                    ) from exc
                when = work_instant(public.at, zone, "at").instant
                if when <= utc_now():
                    raise DocketError(
                        code="followup_time_elapsed",
                        message="Choose a future follow-up instant.",
                        details={"field": "at"},
                    )
                mutation = OperationalReminderCreate(
                    change_id="followup",
                    action="create",
                    object_type="reminder_plan",
                    affected_fields=["subject_ref", "trigger_at"],
                    create_spec=OperationalReminderSpec(
                        subject_ref=public.subject_ref,
                        trigger_at=when,
                        timezone=zone,
                        label=public.label,
                    ),
                )
                observed = context
            else:
                assert isinstance(public, ReconcileWorkload)
                spec = WorkloadFollowupService(self.session, self.settings).capture(
                    public, utc_now()
                )
                versions = dict(context.versions)
                for exception in spec.exceptions:
                    versions.update(exception.basis_versions)
                for ref in spec.preference_refs:
                    versions[ref] = ProvenanceRefService(self.session).get(ref).version
                # Internal subject discovery belongs to this explicit review command.
                # The agent is not required to construct a source/version graph.
                observed = NativeContext(snapshot=context.snapshot, versions=versions)
                mutation = WorkloadReconcile(
                    change_id="review",
                    action="create",
                    object_type="workload_reconciliation",
                    affected_fields=["exceptions"],
                    create_spec=spec,
                )
            plan = mutation_plan(observed, [mutation_input_json(mutation)], command)
            if isinstance(public, ReconcileWorkload):
                plan["command_preview"] = {
                    "as_of": spec.as_of.isoformat(),
                    "exception_count": len(spec.exceptions),
                    "exceptions": [
                        {"category": e.category, "subject_ref": e.subject_ref, "summary": e.summary}
                        for e in spec.exceptions[:3]
                    ],
                    "coverage": spec.coverage,
                    "meaning": "Nonbinding review; no actual work, payment or provider effect.",
                }
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

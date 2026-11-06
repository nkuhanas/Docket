"""Formal timesheets over immutable work calculations and shared claim quantities."""

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.compensation import period, week
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.domain.work_time import work_zone
from docket.models import (
    CompensationEvaluation,
    Engagement,
    FinancialDocument,
    FinancialDocumentRevision,
    NativeContext,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    TimesheetPrepare,
    TimesheetRefresh,
    TimesheetTransition,
    mutation_input_json,
)
from docket.schemas.compensation import GetCompensation, PolicyTermSpec
from docket.schemas.financial_documents import (
    PrepareTimesheet,
    RefreshTimesheet,
    TimesheetSpec,
    TimesheetStatusSpec,
    UpdateTimesheet,
)
from docket.services.business_commands import mutation_plan
from docket.services.compensation_evaluations import CompensationEvaluationService
from docket.services.compensation_policies import (
    CompensationPolicyService,
    date_boundary,
    instant,
    term_spec,
)
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService
from docket.services.work_claims import WorkClaimService

DOCUMENT_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_prepare_timesheet": PrepareTimesheet,
    "docket_update_timesheet": UpdateTimesheet,
    "docket_refresh_timesheet": RefreshTimesheet,
}


def document_error(code: str, message: str, **details: Any) -> DocketError:
    return DocketError(code=code, message=message, details=details)


class FinancialDocumentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.work = WorkAttributionService(session)
        self.claims = WorkClaimService(session)

    def head(self, ref: str, *, lock: bool = False) -> FinancialDocument:
        row = self.work.head(FinancialDocument, ref, lock=lock)
        assert isinstance(row, FinancialDocument)
        return row

    def revision(self, head: FinancialDocument) -> FinancialDocumentRevision:
        row = self.session.scalar(
            select(FinancialDocumentRevision).where(
                FinancialDocumentRevision.document_ref == head.ref_id,
                FinancialDocumentRevision.revision == head.version,
            )
        )
        assert row is not None
        return row

    def evaluation(self, ref: str) -> CompensationEvaluation:
        principal = require_principal("read")
        row = self.session.scalar(
            select(CompensationEvaluation).where(
                CompensationEvaluation.ref_id == ref,
                CompensationEvaluation.operator_ref == principal.operator_ref,
            )
        )
        if row is None:
            raise document_error(
                "financial_basis_unavailable", "Select this connection's calculation."
            )
        return row

    def guard_basis(self, evaluation: CompensationEvaluation, changeset: Any) -> None:
        engagement = self.session.scalar(
            select(Engagement)
            .where(
                Engagement.ref_id == evaluation.parameters_json["engagement_ref"],
                Engagement.operator_ref == require_principal("commit").operator_ref,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if engagement is None:
            raise document_error("financial_basis_unavailable", "Select an owned engagement.")
        self.work.check_version(engagement, changeset)
        captured = {
            line["work_ref"]: line["revision_ref"]
            for line in evaluation.result_json["contributions"]
        }
        heads = self.session.execute(
            select(WorkSession, WorkSessionRevision.ref_id)
            .join(
                WorkSessionRevision,
                (WorkSessionRevision.work_ref == WorkSession.ref_id)
                & (WorkSessionRevision.revision == WorkSession.version),
            )
            .where(
                WorkSession.ref_id.in_(captured),
                WorkSession.operator_ref == engagement.operator_ref,
            )
            .order_by(WorkSession.ref_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        for head, current_ref in heads:
            if current_ref != captured[head.ref_id] or head.superseded:
                raise document_error(
                    "financial_basis_changed",
                    "Refresh the draft from current work.",
                    work_ref=head.ref_id,
                )

    def reconcile_flags(self, revision: FinancialDocumentRevision) -> list[str]:
        evaluation = self.session.scalar(
            select(CompensationEvaluation).where(
                CompensationEvaluation.ref_id == revision.evaluation_ref
            )
        )
        assert evaluation is not None
        captured = {
            line["work_ref"]: line["revision_ref"]
            for line in evaluation.result_json["contributions"]
        }
        current = {
            row[0]: row[1]
            for row in self.session.execute(
                select(WorkSession.ref_id, WorkSessionRevision.ref_id)
                .join(
                    WorkSessionRevision,
                    (WorkSessionRevision.work_ref == WorkSession.ref_id)
                    & (WorkSessionRevision.revision == WorkSession.version),
                )
                .where(WorkSession.ref_id.in_(captured))
            )
        }
        flags = (
            {"work_revision_changed"}
            if any(current.get(ref) != old for ref, old in captured.items())
            else set()
        )
        policy = CompensationPolicyService(self.session)
        eng = evaluation.parameters_json["engagement_ref"]
        lines = evaluation.result_json["contributions"]
        zones = dict(
            self.session.execute(
                select(WorkSessionRevision.ref_id, WorkSessionRevision.timezone).where(
                    WorkSessionRevision.ref_id.in_({line["source_revision_ref"] for line in lines})
                )
            )
            .tuples()
            .all()
        )
        basis = [
            PolicyTermSpec.model_validate(row["terms"])
            for row in evaluation.result_json["basis"]
            if row["engagement_ref"] == eng
        ]

        def coverage(windows: list[PolicyTermSpec], lo: datetime, hi: datetime) -> list[Any]:
            result: list[Any] = []
            for window in windows:
                start, end = max(lo, window.start_at), min(hi, window.end_at or hi)
                if start >= end:
                    continue
                values = window.model_dump(exclude={"start_at", "end_at"})
                if result and result[-1][1] == start and result[-1][2] == values:
                    result[-1] = (result[-1][0], end, values)
                else:
                    result.append((start, end, values))
            return result

        for kind in ("pay", "reporting"):
            head = policy.head(
                eng, kind, evaluation.parameters_json["component_key"] if kind == "pay" else "base"
            )
            windows = (
                [term_spec(row) for row in policy.terms(policy.revision(head).ref_id)]
                if head
                else []
            )
            old_windows = sorted(
                (window for window in basis if window.kind == kind), key=lambda w: w.start_at
            )
            for line in lines:
                if line.get("start") and line.get("end"):
                    lo, hi = (
                        instant(datetime.fromisoformat(line["start"])),
                        instant(datetime.fromisoformat(line["end"])),
                    )
                elif line.get("day"):
                    day = date.fromisoformat(line["day"])
                    zone = zones[line["source_revision_ref"]]
                    lo = date_boundary(day, zone, "day")
                    hi = date_boundary(day + timedelta(days=1), zone, "day")
                else:
                    lo = date_boundary(revision.date_from, revision.timezone, "date_from")
                    hi = date_boundary(revision.date_to, revision.timezone, "date_to")
                old, new = coverage(old_windows, lo, hi), coverage(windows, lo, hi)
                if old != new:
                    flags.add("policy_basis_changed" if old and new else "policy_coverage_changed")
        return sorted(flags)

    def view(
        self, head: FinancialDocument, revision: FinancialDocumentRevision | None = None
    ) -> dict[str, Any]:
        revision = revision or self.revision(head)
        return {
            "ref": head.ref_id,
            "version": revision.revision,
            "kind": head.kind,
            "status": revision.status,
            "engagement_ref": head.engagement_ref,
            "component_key": head.component_key,
            "revision_ref": revision.ref_id,
            "previous_revision_ref": self.session.scalar(
                select(FinancialDocumentRevision.ref_id).where(
                    FinancialDocumentRevision.document_ref == head.ref_id,
                    FinancialDocumentRevision.revision == revision.revision - 1,
                )
            )
            if revision.revision > 1
            else None,
            "revision_count": head.version,
            "date_from": revision.date_from.isoformat(),
            "date_to": revision.date_to.isoformat(),
            "timezone": revision.timezone,
            "evaluation_ref": revision.evaluation_ref,
            "summary": revision.snapshot_json["summary"],
            "claim_count": len(revision.snapshot_json["claim_refs"]),
            "reconciliation_flags": self.reconcile_flags(revision),
            "payment_state": "independent; submission does not establish payment",
            "detail": {
                "ref": revision.ref_id,
                "view": "audit",
                "representation": "document_json_chunk",
            },
        }

    def apply(self, session: Session, changeset: Any, change: Any) -> list[str]:
        head = self.head(change.object_ref, lock=True) if change.object_ref else None
        if head:
            self.work.check_version(head, changeset)
            if head.kind != "timesheet":
                raise document_error("wrong_document_kind", "Select a timesheet.")
        previous = self.revision(head) if head else None
        if change.mutation_type == "timesheet_transition":
            assert head is not None and previous is not None
            spec = TimesheetStatusSpec.model_validate(change.payload)
            allowed = {
                "draft": {"verified", "submitted", "cancelled"},
                "verified": {"submitted", "cancelled"},
                "submitted": {"approved", "rejected"},
            }
            if spec.status not in allowed.get(head.status, set()):
                raise document_error(
                    "invalid_timesheet_transition",
                    "Choose an action for the current timesheet state.",
                    status=head.status,
                )
            evaluation = self.evaluation(previous.evaluation_ref)
            if spec.status in {"verified", "submitted"}:
                self.guard_basis(evaluation, changeset)
                blocking = set(previous.snapshot_json["summary"]["flags"]) & {
                    "open_timer",
                    "quantity_unknown",
                    "unplaced_time",
                    "cross_period_quantity_unallocated",
                    "overlapping_work",
                    "quantity_lineage_needs_review",
                    "outside_engagement_dates",
                    "claimed_work_attribution_changed",
                }
                if blocking:
                    raise document_error(
                        "timesheet_needs_reconciliation",
                        "Resolve the affected hours before finalizing this timesheet.",
                        flags=sorted(blocking),
                    )
            head.version += 1
            head.status = spec.status
            head.updated_at = utc_now()
            self.session.flush()
            revision = FinancialDocumentRevision(
                document_ref=head.ref_id,
                revision=head.version,
                status=head.status,
                evaluation_ref=previous.evaluation_ref,
                date_from=previous.date_from,
                date_to=previous.date_to,
                timezone=previous.timezone,
                snapshot_json=previous.snapshot_json,
                observation=spec.note,
                observation_at=spec.observed_at,
                amends_ref=previous.amends_ref,
                basis_refs=list(change.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(revision)
            self.session.flush()
            self.claims.transition(head, revision)
        else:
            prepared = TimesheetSpec.model_validate(change.create_spec or change.payload)
            evaluation = self.evaluation(prepared.evaluation_ref)
            self.guard_basis(evaluation, changeset)
            if head and head.status not in {"draft", "verified"}:
                raise document_error(
                    "timesheet_snapshot_final", "Prepare a new amendment for a submitted timesheet."
                )
            if prepared.amends_ref:
                parent = self.head(prepared.amends_ref)
                if parent.engagement_ref != prepared.engagement_ref or parent.kind != "timesheet":
                    raise document_error(
                        "invalid_timesheet_amendment", "Choose a related owned timesheet."
                    )
            if head is None:
                head = FinancialDocument(
                    ref_id=new_public_ref("fdoc"),
                    operator_ref=require_principal("commit").operator_ref,
                    engagement_ref=prepared.engagement_ref,
                    component_key=prepared.component_key,
                    kind="timesheet",
                    status="draft",
                    version=1,
                    created_by_changeset_ref=changeset.ref_id,
                )
                self.session.add(head)
                self.session.flush()
            else:
                head.version += 1
                head.status = "draft"
                head.updated_at = utc_now()
            plan = self.claims.plan(head, evaluation, changeset.ref_id)
            summary = {
                **evaluation.result_json["summary"],
                "financial_state": "claim basis; document lifecycle and payment are separate",
                "flags": sorted(set(evaluation.result_json["summary"]["flags"]) | plan.flags),
            }
            revision = FinancialDocumentRevision(
                document_ref=head.ref_id,
                revision=head.version,
                status="draft",
                evaluation_ref=evaluation.ref_id,
                date_from=date.fromisoformat(evaluation.parameters_json["date_from"]),
                date_to=date.fromisoformat(evaluation.parameters_json["date_to"]),
                timezone=evaluation.parameters_json["timezone"],
                snapshot_json={
                    "engagement_label": self.work.engagement_spec(
                        self.work.head(Engagement, head.engagement_ref)
                    ).label,
                    "summary": summary,
                    "claim_refs": plan.refs,
                    "reserved_raw_us": sum(row.end_us - row.start_us for row in plan.new),
                    "reused_claim_count": len(plan.shared_refs),
                    "calculation_version": evaluation.calculation_version,
                    "work_report_ref": evaluation.work_report_ref,
                },
                amends_ref=prepared.amends_ref,
                basis_refs=list(change.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(revision)
            self.session.flush()
            self.claims.apply(plan, revision)
        self.work.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id, revision.ref_id]


class FinancialDocumentCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = (
            PrepareTimesheet.model_validate(arguments)
            if command == "docket_prepare_timesheet"
            else UpdateTimesheet.model_validate(arguments)
            if command == "docket_update_timesheet"
            else RefreshTimesheet.model_validate(arguments)
        )
        values = public.model_dump(mode="json", exclude_unset=True)
        service = FinancialDocumentService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            head = (
                None if isinstance(public, PrepareTimesheet) else service.head(public.timesheet_ref)
            )
            if isinstance(public, UpdateTimesheet):
                observed = public.observed_at or utc_now()
                if observed.tzinfo is None or observed > utc_now():
                    raise document_error(
                        "invalid_financial_observation",
                        "Use a past offset timestamp.",
                        field="observed_at",
                    )
                action: Any = TimesheetTransition(
                    change_id="timesheet",
                    action="update",
                    object_type="financial_document",
                    object_ref=public.timesheet_ref,
                    affected_fields=["status"],
                    payload=TimesheetStatusSpec(
                        status=public.status, note=public.note, observed_at=instant(observed)
                    ),
                )
                assert head is not None
                retained = {head.engagement_ref}
            else:
                if isinstance(public, PrepareTimesheet):
                    eng, component, timezone = (
                        public.engagement_ref,
                        public.component_key,
                        public.timezone or self.settings.timezone,
                    )
                    first, last = public.date_from, public.date_to
                    now = utc_now()
                    policy = CompensationPolicyService(self.session)
                    rules = policy.head(eng, "reporting", "base")
                    active = (
                        next(
                            (
                                term_spec(term)
                                for term in policy.terms(policy.revision(rules).ref_id)
                                if instant(term.start_at) <= now
                                and (term.end_at is None or now < instant(term.end_at))
                            ),
                            None,
                        )
                        if rules
                        else None
                    )
                    timezone = public.timezone or (
                        active.timezone if active else self.settings.timezone
                    )
                    today = now.astimezone(work_zone(timezone)).date()
                    if first is None and last is None:
                        fallback = week(today, (active.week_starts_on or 0) if active else 0)
                        first, last = period(today, active, fallback, fallback + timedelta(days=7))
                    elif first is None or last is None:
                        raise document_error(
                            "invalid_timesheet_period", "Supply both bounds or omit both."
                        )
                    amends = public.amends_ref
                else:
                    assert head is not None
                    previous = service.revision(head)
                    eng, component = head.engagement_ref, head.component_key
                    first, last, timezone = previous.date_from, previous.date_to, previous.timezone
                    amends = previous.amends_ref
                evaluation = CompensationEvaluationService(self.session, self.settings).capture(
                    GetCompensation(
                        engagement_ref=eng,
                        component_key=component,
                        date_from=first,
                        date_to=last,
                        timezone=timezone,
                    )
                )
                spec = TimesheetSpec(
                    engagement_ref=eng,
                    component_key=component,
                    evaluation_ref=evaluation.ref_id,
                    amends_ref=amends,
                )
                action = (
                    TimesheetPrepare(
                        change_id="timesheet",
                        action="create",
                        object_type="financial_document",
                        affected_fields=["timesheet"],
                        create_spec=spec,
                    )
                    if head is None
                    else TimesheetRefresh(
                        change_id="timesheet",
                        action="update",
                        object_type="financial_document",
                        object_ref=head.ref_id,
                        affected_fields=["timesheet"],
                        payload=spec,
                    )
                )
                retained = {evaluation.ref_id}
            plan = mutation_plan(
                context, [mutation_input_json(action)], command, retained_refs=retained
            )
            if head is not None and isinstance(public, UpdateTimesheet):
                plan["expected_versions"][head.engagement_ref] = context.versions.get(
                    head.engagement_ref
                )
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

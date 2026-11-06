"""Canonical invoice preparation, immutable issuance and reported manual delivery."""

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.public_refs import new_public_ref
from docket.domain.work_time import work_zone
from docket.models import (
    Engagement,
    Expense,
    ExpenseRevision,
    FinancialDocument,
    FinancialDocumentObservation,
    FinancialDocumentRevision,
    InvoiceNumber,
    NativeContext,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    InvoicePrepare,
    InvoiceRefresh,
    InvoiceTransition,
    mutation_input_json,
)
from docket.schemas.compensation import GetCompensation
from docket.schemas.invoices import (
    InvoiceFields,
    InvoiceSpec,
    InvoiceStatusSpec,
    PrepareInvoice,
    RefreshInvoice,
    UpdateInvoice,
)
from docket.services.business_commands import mutation_plan
from docket.services.compensation_evaluations import CompensationEvaluationService
from docket.services.compensation_policies import instant
from docket.services.document_allocations import DocumentAllocationService
from docket.services.expenses import ExpenseService
from docket.services.financial_documents import FinancialDocumentService, document_error
from docket.services.invoice_claims import InvoiceClaimPlanner
from docket.services.native_commands import NativeCommandService

INVOICE_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_prepare_invoice": PrepareInvoice,
    "docket_refresh_invoice": RefreshInvoice,
    "docket_update_invoice": UpdateInvoice,
}


class InvoiceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.documents = FinancialDocumentService(session)
        self.work = self.documents.work
        self.claims = self.documents.claims
        self.allocations = DocumentAllocationService(session)
        self.planner = InvoiceClaimPlanner(session)

    def head(self, ref: str, *, lock: bool = False) -> FinancialDocument:
        row = self.documents.head(ref, lock=lock)
        if row.kind != "invoice":
            raise document_error("wrong_document_kind", "Select an invoice.")
        return row

    @staticmethod
    def work_refs(revision: FinancialDocumentRevision) -> set[str]:
        return {
            line["work_ref"]
            for line in revision.snapshot_json.get("lines", [])
            if line.get("work_ref")
        }

    def flags(self, revision: FinancialDocumentRevision) -> list[str]:
        flags = set(self.documents.reconcile_flags(revision, self.work_refs(revision)))
        for ref, old in revision.snapshot_json.get("expense_revisions", {}).items():
            head = ExpenseService(self.session).head(ref)
            if head.retracted or ExpenseService(self.session).revision(head).ref_id != old:
                flags.add("expense_revision_changed")
        return sorted(flags)

    def view(self, head: FinancialDocument, revision: FinancialDocumentRevision) -> dict[str, Any]:
        body = revision.snapshot_json
        header = body["header"]
        number = self.session.get(InvoiceNumber, head.ref_id)
        observations = list(
            self.session.scalars(
                select(FinancialDocumentObservation)
                .where(
                    FinancialDocumentObservation.document_ref == head.ref_id,
                )
                .order_by(
                    FinancialDocumentObservation.created_at.desc(),
                    FinancialDocumentObservation.ref_id.desc(),
                )
                .limit(4)
            )
        )
        return {
            "ref": head.ref_id,
            "version": revision.revision,
            "revision_ref": revision.ref_id,
            "kind": "invoice",
            "status": revision.status,
            "engagement_ref": head.engagement_ref,
            "component_key": head.component_key,
            "date_from": revision.date_from.isoformat(),
            "date_to": revision.date_to.isoformat(),
            "timezone": revision.timezone,
            "evaluation_ref": revision.evaluation_ref,
            "summary": body["summary"],
            "claim_count": len(body["claim_refs"]),
            "allocation_count": len(body["allocation_refs"]),
            "number": number.number if number and revision.status == "issued" else None,
            "due_on": header.get("due_on"),
            "client_label": header["client_label"][:200],
            "reconciliation_flags": self.flags(revision),
            "revision_count": head.version,
            "delivery_observations": [
                {
                    "ref": row.ref_id,
                    "kind": row.kind,
                    "observed_at": instant(row.observed_at).isoformat()
                    if row.observed_at
                    else None,
                }
                for row in observations[:3]
                if revision.status == "issued"
            ],
            "delivery_observations_truncated": len(observations) > 3,
            "payment_state": "independent; issuance and delivery do not establish payment",
            "detail": {"tool": "docket_read_document", "ref": revision.ref_id},
        }

    def apply(self, changeset: Any, change: Any) -> list[str]:
        head = self.head(change.object_ref, lock=True) if change.object_ref else None
        if head:
            self.work.check_version(head, changeset)
        previous = self.documents.revision(head) if head else None
        if change.mutation_type == "invoice_transition":
            assert head is not None and previous is not None
            return self.transition(
                head, previous, InvoiceStatusSpec.model_validate(change.payload), changeset, change
            )
        spec = InvoiceSpec.model_validate(change.create_spec or change.payload)
        if head and head.status != "draft":
            raise document_error(
                "invoice_snapshot_final", "Issued/cancelled invoices cannot be refreshed."
            )
        if head and spec.document_ref != head.ref_id:
            raise document_error("invoice_target_changed", "Refresh the selected invoice.")
        evaluation = self.documents.evaluation(spec.evaluation_ref)
        if (
            evaluation.parameters_json.get("engagement_ref") != spec.engagement_ref
            or evaluation.parameters_json.get("component_key") != spec.component_key
            or (
                head is not None
                and (
                    head.engagement_ref != spec.engagement_ref
                    or head.component_key != spec.component_key
                )
            )
        ):
            raise document_error(
                "financial_basis_unavailable",
                "Use the selected engagement and component's calculation.",
            )
        selected = set(spec.parameters.work_refs) if spec.parameters.work_refs is not None else None
        self.documents.guard_basis(evaluation, changeset, selected)
        if spec.amends_ref:
            parent = self.head(spec.amends_ref)
            if parent.engagement_ref != spec.engagement_ref:
                raise document_error(
                    "invoice_amendment_unrelated", "Select the same engagement's invoice."
                )
        if head is None:
            head = FinancialDocument(
                ref_id=spec.document_ref,
                operator_ref=require_principal("commit").operator_ref,
                engagement_ref=spec.engagement_ref,
                component_key=spec.component_key,
                kind="invoice",
                status="draft",
                version=1,
                created_by_changeset_ref=changeset.ref_id,
            )
        plan = self.planner.plan(head, evaluation, spec, changeset.ref_id)
        if previous:
            head.version += 1
            head.updated_at = utc_now()
        else:
            self.session.add(head)
        self.session.flush()
        engagement = self.work.engagement_spec(self.work.head(Engagement, spec.engagement_ref))
        header = spec.parameters.model_dump(mode="json")
        header.update(
            client_label=header.get("client_label") or engagement.label,
            issuer_label=header.get("issuer_label") or head.operator_ref,
            payer_ref=next(iter(plan.payer_refs))
            if len(plan.payer_refs) == 1
            else engagement.payer_ref,
            payer_refs=sorted(plan.payer_refs),
            organization_ref=engagement.organization_ref,
            number_series=header.get("number_series") or "INV",
        )
        revision = FinancialDocumentRevision(
            document_ref=head.ref_id,
            revision=head.version,
            status="draft",
            evaluation_ref=evaluation.ref_id,
            date_from=date.fromisoformat(evaluation.parameters_json["date_from"]),
            date_to=date.fromisoformat(evaluation.parameters_json["date_to"]),
            timezone=evaluation.parameters_json["timezone"],
            snapshot_json={
                "engagement_label": engagement.label,
                "header": header,
                "summary": plan.summary(),
                "claim_refs": plan.claims.refs,
                "allocation_refs": [row.ref_id for row in plan.allocations],
                "lines": plan.lines,
                "expense_revisions": spec.expense_revisions,
                "excluded_work_refs": sorted(plan.excluded_work_refs),
                "parameters": spec.parameters.model_dump(mode="json"),
                "calculation_version": evaluation.calculation_version,
                "invoice_calculation_version": "invoice-v1",
                "work_report_ref": evaluation.work_report_ref,
            },
            amends_ref=spec.amends_ref,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(revision)
        self.session.flush()
        if previous:
            self.allocations.transition(head.ref_id, "released", revision)
        self.claims.apply(plan.claims, revision)
        self.allocations.persist(plan.allocations, revision)
        self.work.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id, revision.ref_id]

    def transition(
        self,
        head: FinancialDocument,
        previous: FinancialDocumentRevision,
        spec: InvoiceStatusSpec,
        changeset: Any,
        change: Any,
    ) -> list[str]:
        for name, value in (("observed_at", spec.observed_at), ("issued_at", spec.issued_at)):
            if value is not None and (value.tzinfo is None or value > utc_now()):
                raise document_error(
                    "invalid_invoice_observation", "Use a past offset timestamp.", field=name
                )
        if spec.status == "issued" and spec.issued_at is None:
            raise document_error(
                "invoice_issue_instant_required", "Capture the issue instant in the exact proposal."
            )
        if spec.status == "delivered":
            if head.status != "issued":
                raise document_error(
                    "invoice_not_issued", "Record delivery only for an issued invoice."
                )
            observation = FinancialDocumentObservation(
                document_ref=head.ref_id,
                document_revision_ref=previous.ref_id,
                observed_at=spec.observed_at,
                note=spec.note,
                basis_refs=list(change.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(observation)
            self.session.flush()
            self.work.audit(head, observation.ref_id, changeset, change)
            return [head.ref_id, observation.ref_id]
        if head.status != "draft":
            raise document_error(
                "invoice_snapshot_final",
                "An issued invoice needs an explicit credit or adjustment.",
            )
        if spec.status == "issued":
            evaluation = self.documents.evaluation(previous.evaluation_ref)
            self.documents.guard_basis(evaluation, changeset, self.work_refs(previous))
            flags = set(previous.snapshot_json["summary"]["flags"]) | set(self.flags(previous))
            if flags:
                raise document_error(
                    "invoice_needs_reconciliation",
                    "Resolve the affected invoice basis before issuing.",
                    flags=sorted(flags),
                )
        head.version += 1
        head.status = spec.status
        head.updated_at = utc_now()
        self.session.flush()
        body = {**previous.snapshot_json}
        if spec.status == "issued":
            body["issued_at"] = spec.issued_at.isoformat() if spec.issued_at else None
        revision = FinancialDocumentRevision(
            document_ref=head.ref_id,
            revision=head.version,
            status=head.status,
            evaluation_ref=previous.evaluation_ref,
            date_from=previous.date_from,
            date_to=previous.date_to,
            timezone=previous.timezone,
            snapshot_json=body,
            observation=spec.note,
            observation_at=spec.observed_at,
            amends_ref=previous.amends_ref,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(revision)
        self.session.flush()
        self.allocations.transition(
            head.ref_id, "issued" if spec.status == "issued" else "released", revision
        )
        self.claims.transition(head, revision)
        if spec.status == "issued":
            self.allocations.number(revision, previous.snapshot_json["header"]["number_series"])
        self.work.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id, revision.ref_id]


class InvoiceCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = INVOICE_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)
        service = InvoiceService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            assert isinstance(public, PrepareInvoice | RefreshInvoice | UpdateInvoice)
            head = None if isinstance(public, PrepareInvoice) else service.head(public.invoice_ref)
            if isinstance(public, UpdateInvoice):
                if public.observed_at and (
                    public.observed_at.tzinfo is None or public.observed_at > utc_now()
                ):
                    raise document_error(
                        "invalid_invoice_observation",
                        "Use a past offset timestamp or omit unknown time.",
                        field="observed_at",
                    )
                action: Any = InvoiceTransition(
                    change_id="invoice",
                    action="update",
                    object_type="financial_document",
                    object_ref=public.invoice_ref,
                    affected_fields=[
                        "status" if public.status != "delivered" else "reported_delivery"
                    ],
                    payload=InvoiceStatusSpec(
                        status=public.status,
                        note=public.note,
                        observed_at=instant(public.observed_at) if public.observed_at else None,
                        issued_at=utc_now() if public.status == "issued" else None,
                    ),
                )
                retained = {head.engagement_ref} if head else set()
            else:
                assert isinstance(public, PrepareInvoice | RefreshInvoice)
                if head:
                    previous = service.documents.revision(head)
                    parameters = InvoiceFields.model_validate(
                        {
                            **previous.snapshot_json["parameters"],
                            **{
                                key: value
                                for key, value in values.items()
                                if key in InvoiceFields.model_fields
                            },
                        }
                    )
                    eng, component, amends = (
                        head.engagement_ref,
                        head.component_key,
                        previous.amends_ref,
                    )
                    document_ref = head.ref_id
                else:
                    assert isinstance(public, PrepareInvoice)
                    parameters = InvoiceFields.model_validate(
                        {
                            key: value
                            for key, value in values.items()
                            if key in InvoiceFields.model_fields
                        }
                    )
                    eng, component, amends = (
                        public.engagement_ref,
                        public.component_key,
                        public.amends_ref,
                    )
                    document_ref = new_public_ref("fdoc")
                parameters = self.period(parameters, eng, service)
                evaluation = CompensationEvaluationService(self.session, self.settings).capture(
                    GetCompensation(
                        engagement_ref=eng,
                        component_key=component,
                        date_from=parameters.date_from,
                        date_to=parameters.date_to,
                        timezone=parameters.timezone,
                    )
                )
                if parameters.work_refs is not None:
                    available = {row["work_ref"] for row in evaluation.result_json["contributions"]}
                    missing = set(parameters.work_refs) - available
                    if missing:
                        raise document_error(
                            "invoice_work_selection_unavailable",
                            "Select work in this engagement and period.",
                            field="work_refs",
                            refs=sorted(missing),
                        )
                fees = self.expenses(parameters, eng)
                _, _, _, basis = service.planner.sources(
                    evaluation, parameters, document_ref, list(fees), lock=False
                )
                spec = InvoiceSpec(
                    document_key=document_ref.split("_", 1)[1],
                    engagement_ref=eng,
                    component_key=component,
                    evaluation_ref=evaluation.ref_id,
                    parameters=parameters,
                    expense_revisions=fees,
                    financial_basis=basis,
                    amends_ref=amends,
                )
                if amends:
                    parent = service.head(amends)
                    if parent.engagement_ref != eng:
                        raise document_error(
                            "invoice_amendment_unrelated", "Select the same engagement's invoice."
                        )
                variant = InvoiceRefresh if head else InvoicePrepare
                action = variant.model_validate(
                    {
                        "change_id": "invoice",
                        "action": "update" if head else "create",
                        "object_type": "financial_document",
                        "object_ref": head.ref_id if head else None,
                        "affected_fields": ["invoice"],
                        "payload" if head else "create_spec": spec,
                    }
                )
                retained = {
                    document_ref,
                    evaluation.ref_id,
                    *fees,
                    *fees.values(),
                    *(parameters.work_refs or []),
                }
                if amends:
                    retained.add(amends)
            plan = mutation_plan(
                context, [mutation_input_json(action)], command, retained_refs=retained
            )
            if head is not None and isinstance(public, UpdateInvoice) and public.status == "issued":
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

    def period(self, parameters: InvoiceFields, eng: str, service: InvoiceService) -> InvoiceFields:
        zone = parameters.timezone or self.settings.timezone
        today = utc_now().astimezone(work_zone(zone)).date()
        first = parameters.date_from
        last = parameters.date_to or today + timedelta(days=1)
        if first is None:
            engagement = service.work.engagement_spec(service.work.head(Engagement, eng))
            dates = [engagement.effective_start] if engagement.effective_start else []
            rows = self.session.scalars(
                select(WorkSessionRevision)
                .join(
                    WorkSession,
                    (WorkSession.ref_id == WorkSessionRevision.work_ref)
                    & (WorkSession.version == WorkSessionRevision.revision),
                )
                .where(
                    WorkSession.operator_ref == require_principal("read").operator_ref,
                    WorkSessionRevision.engagement_ref == eng,
                    WorkSession.superseded.is_(False),
                )
            )
            for row in rows:
                day = (
                    row.work_date
                    or row.period_start
                    or (
                        instant(row.start_at).astimezone(work_zone(zone)).date()
                        if row.start_at
                        else None
                    )
                )
                if day:
                    dates.append(day)
            first_expense = self.session.scalar(
                select(func.min(ExpenseRevision.incurred_on))
                .join(
                    Expense,
                    (Expense.ref_id == ExpenseRevision.expense_ref)
                    & (Expense.version == ExpenseRevision.revision),
                )
                .where(
                    Expense.operator_ref == require_principal("read").operator_ref,
                    Expense.retracted.is_(False),
                    ExpenseRevision.engagement_ref == eng,
                    ExpenseRevision.reimbursable.is_(True),
                )
            )
            if first_expense:
                dates.append(first_expense)
            first = min(dates) if dates else today.replace(day=1)
        if first >= last:
            raise document_error(
                "invalid_invoice_period", "date_to must follow date_from.", field="date_to"
            )
        return parameters.model_copy(update={"date_from": first, "date_to": last, "timezone": zone})

    def expenses(self, parameters: InvoiceFields, eng: str) -> dict[str, str]:
        service = ExpenseService(self.session)
        if parameters.expense_refs is not None:
            rows = [
                service.revision(service.head(ref))
                for ref in dict.fromkeys(parameters.expense_refs)
            ]
        else:
            rows = list(
                self.session.scalars(
                    select(ExpenseRevision)
                    .join(
                        Expense,
                        (Expense.ref_id == ExpenseRevision.expense_ref)
                        & (Expense.version == ExpenseRevision.revision),
                    )
                    .where(
                        Expense.operator_ref == require_principal("read").operator_ref,
                        Expense.retracted.is_(False),
                        ExpenseRevision.engagement_ref == eng,
                        ExpenseRevision.incurred_on >= parameters.date_from,
                        ExpenseRevision.incurred_on < parameters.date_to,
                        ExpenseRevision.reimbursable.is_(True),
                    )
                    .order_by(Expense.ref_id)
                )
            )
        return {row.expense_ref: row.ref_id for row in rows}

"""Canonical reported costs; optional receipt capture never gates valid recording."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    Engagement,
    Entity,
    Expense,
    ExpenseObservation,
    ExpenseRevision,
    NativeContext,
    Source,
)
from docket.models.base import utc_now
from docket.schemas.authority import ExpenseCreate, ExpenseModify, mutation_input_json
from docket.schemas.expenses import ExpenseSpec, RecordExpense, UpdateExpense
from docket.services.business_commands import _named_refs, mutation_plan
from docket.services.compensation_policies import instant
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService

EXPENSE_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_record_expense": RecordExpense,
    "docket_update_expense": UpdateExpense,
}
OBSERVATION_FIELDS = {"submission_status", "claim_amount", "claim_amount_supplied", "observed_at"}
IDENTITY_FIELDS = {"external_source", "external_id"}
DATA_FIELDS = set(ExpenseSpec.model_fields) - OBSERVATION_FIELDS - IDENTITY_FIELDS


def decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def eligible(spec: ExpenseSpec) -> Decimal | None:
    if spec.reimbursable is None:
        return None
    return (
        (spec.reimbursable_amount if spec.reimbursable_amount is not None else spec.amount)
        if spec.reimbursable
        else Decimal(0)
    )


class ExpenseService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.work = WorkAttributionService(session)

    def head(self, ref: str, *, lock: bool = False) -> Expense:
        statement = select(Expense).where(
            Expense.ref_id == ref, Expense.operator_ref == require_principal("read").operator_ref
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalar(statement)
        if row is None:
            raise DocketError(
                code="expense_unavailable",
                message="Select an owned expense.",
                details={"field": "expense_ref"},
            )
        return row

    def revision(self, head: Expense) -> ExpenseRevision:
        row = self.session.scalar(
            select(ExpenseRevision).where(
                ExpenseRevision.expense_ref == head.ref_id, ExpenseRevision.revision == head.version
            )
        )
        assert row is not None
        return row

    def spec(self, head: Expense, row: ExpenseRevision | None = None) -> ExpenseSpec:
        row = row or self.revision(head)
        return ExpenseSpec.model_validate(
            {
                **{key: getattr(row, key) for key in DATA_FIELDS},
                "external_source": head.external_source,
                "external_id": head.external_id,
            }
        )

    def observation(self, ref: str, *, approved: bool = False) -> ExpenseObservation | None:
        statement = select(ExpenseObservation).where(ExpenseObservation.expense_ref == ref)
        if approved:
            statement = statement.where(ExpenseObservation.status == "approved")
        return self.session.scalar(statement.order_by(ExpenseObservation.version.desc()).limit(1))

    def normalize(self, spec: ExpenseSpec) -> ExpenseSpec:
        fields = spec.model_dump()
        if spec.observed_at is not None:
            if spec.observed_at.tzinfo is None or spec.observed_at > utc_now():
                raise DocketError(
                    code="invalid_expense_observation_time",
                    message="Use a past offset timestamp or omit an unknown decision time.",
                    details={"field": "observed_at"},
                )
            fields["observed_at"] = instant(spec.observed_at)
        for key, kinds in {
            "organization_ref": {"organization", "institution"},
            "project_ref": {"project"},
            "payer_ref": {"person", "organization", "institution"},
            "owner_ref": {"person", "organization", "institution"},
        }.items():
            ref = getattr(spec, key)
            if ref:
                entity = self.session.scalar(select(Entity).where(Entity.ref_id == ref))
                if (
                    entity is None
                    or entity.canonical_status == "retracted"
                    or entity.entity_kind not in kinds
                ):
                    raise DocketError(
                        code="invalid_expense_reference",
                        message="Select an available reference of the required kind.",
                        details={"field": key, "allowed_kinds": sorted(kinds)},
                    )
        if spec.engagement_ref:
            self.work.head(Engagement, spec.engagement_ref)
        if spec.receipt_ref:
            source = self.session.scalar(
                select(Source.ref_id).where(Source.ref_id == spec.receipt_ref)
            )
            if source:
                fields["receipt_status"] = "available"
                if fields["receipt_note"] == "Reported receipt source was unavailable.":
                    fields["receipt_note"] = None
            else:
                fields.update(
                    receipt_ref=None,
                    receipt_status="unavailable",
                    receipt_note=spec.receipt_note or "Reported receipt source was unavailable.",
                )
        return ExpenseSpec.model_validate(fields)

    def view(self, head: Expense) -> dict[str, Any]:
        row = self.revision(head)
        spec = self.spec(head, row)
        report = self.observation(head.ref_id)
        approval = self.observation(head.ref_id, approved=True)
        flags = []
        if spec.incurred_on is None:
            flags.append("date_unknown")
        if spec.reimbursable is None:
            flags.append("reimbursement_eligibility_unknown")
        if spec.receipt_required and spec.receipt_status != "available":
            flags.append("required_receipt_unavailable")
        if approval:
            basis = self.session.scalar(
                select(ExpenseRevision).where(ExpenseRevision.ref_id == approval.basis_revision_ref)
            )
            assert basis is not None
            if self.spec(head, basis).model_dump(include=DATA_FIELDS) != spec.model_dump(
                include=DATA_FIELDS
            ):
                flags.append("expense_changed_since_approval")
        expected = eligible(spec)
        value = spec.model_dump(
            mode="json", exclude_none=True, exclude=IDENTITY_FIELDS | OBSERVATION_FIELDS
        )
        value.update(
            ref=head.ref_id,
            version=head.version,
            revision_ref=row.ref_id,
            description=spec.description[:200],
            eligible_amount=decimal_text(expected) if expected is not None else None,
            submission_status=report.status if report else "not_submitted",
            flags=flags,
            payment_state="independent; approval is not reimbursement received",
            history={"ref": row.ref_id, "view": "audit", "representation": "expense_json_chunk"},
        )
        possible_duplicates = self.session.scalars(
            select(Expense.ref_id)
            .join(
                ExpenseRevision,
                (ExpenseRevision.expense_ref == Expense.ref_id)
                & (ExpenseRevision.revision == Expense.version),
            )
            .where(
                Expense.operator_ref == head.operator_ref,
                Expense.ref_id != head.ref_id,
                Expense.retracted.is_(False),
                ExpenseRevision.amount == row.amount,
                ExpenseRevision.currency == row.currency,
                ExpenseRevision.incurred_on == row.incurred_on,
                ExpenseRevision.vendor == row.vendor,
            )
            .order_by(Expense.ref_id)
            .limit(5)
        ).all()
        if possible_duplicates:
            value["possible_duplicate_refs"] = list(possible_duplicates)
            flags.append("possible_duplicate; similarity is not identity")
        if report:
            value.update(
                report_observation_ref=report.ref_id,
                report_basis_ref=report.basis_revision_ref,
                reported_claim_amount=report.amount,
                reported_claim_currency=report.currency,
            )
        if approval:
            value.update(
                approval_observation_ref=approval.ref_id,
                approved_basis_ref=approval.basis_revision_ref,
                approved_amount=approval.amount,
                approved_currency=approval.currency,
            )
        return value

    def external(self, spec: ExpenseSpec, *, lock: bool = False) -> Expense | None:
        if spec.external_id is None:
            return None
        key = sha256_json([spec.external_source, spec.external_id])
        owner = require_principal("read").operator_ref
        if lock and self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "expense:" + owner + ":" + key},
            )
        return self.session.scalar(
            select(Expense).where(Expense.operator_ref == owner, Expense.external_key_hash == key)
        )

    def apply(self, session: Session, changeset: Any, change: Any) -> list[str]:
        spec = ExpenseSpec.model_validate(change.create_spec or change.payload)
        spec = self.normalize(spec)
        head = self.head(change.object_ref, lock=True) if change.object_ref else None
        if head:
            self.work.check_version(head, changeset)
        else:
            existing = self.external(spec, lock=True)
            if existing:
                old = self.spec(existing)
                excluded = {
                    "retracted",
                    "source_method",
                    "uncertainty",
                    "receipt_ref",
                    "receipt_status",
                    "receipt_note",
                }
                changed = [
                    key for key in DATA_FIELDS - excluded if getattr(old, key) != getattr(spec, key)
                ]
                if spec.submission_status:
                    observation = self.observation(existing.ref_id)
                    requested_amount = (
                        spec.claim_amount if spec.claim_amount_supplied else eligible(spec)
                    )
                    if (
                        observation is None
                        or observation.status != spec.submission_status
                        or observation.currency != spec.currency
                        or (Decimal(observation.amount) if observation.amount is not None else None)
                        != requested_amount
                        or (
                            spec.observed_at is not None
                            and (
                                instant(observation.observed_at)
                                if observation.observed_at
                                else None
                            )
                            != spec.observed_at
                        )
                    ):
                        changed.append("submission_status")
                if changed:
                    raise DocketError(
                        code="expense_external_identity_conflict",
                        message="This transaction exists. Correct its existing expense.",
                        details={"expense_ref": existing.ref_id, "fields": sorted(changed)},
                    )
                return [existing.ref_id, self.revision(existing).ref_id]
        if head is None:
            head = Expense(
                operator_ref=require_principal("commit").operator_ref,
                version=1,
                retracted=spec.retracted,
                external_source=spec.external_source,
                external_id=spec.external_id,
                external_key_hash=sha256_json([spec.external_source, spec.external_id])
                if spec.external_id
                else None,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(head)
        else:
            head.version += 1
            head.retracted = spec.retracted
            head.updated_at = utc_now()
        self.session.flush()
        fields = spec.model_dump(include=DATA_FIELDS)
        fields["amount"] = decimal_text(spec.amount)
        fields["reimbursable_amount"] = (
            decimal_text(spec.reimbursable_amount) if spec.reimbursable_amount is not None else None
        )
        revision = ExpenseRevision(
            expense_ref=head.ref_id,
            revision=head.version,
            **fields,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(revision)
        self.session.flush()
        if spec.submission_status:
            amount = spec.claim_amount if spec.claim_amount_supplied else eligible(spec)
            self.session.add(
                ExpenseObservation(
                    expense_ref=head.ref_id,
                    version=head.version,
                    basis_revision_ref=revision.ref_id,
                    status=spec.submission_status,
                    amount=decimal_text(amount) if amount is not None else None,
                    currency=spec.currency,
                    observed_at=spec.observed_at,
                    created_by_changeset_ref=changeset.ref_id,
                )
            )
            self.session.flush()
        self.work.audit(head, revision.ref_id, changeset, change)
        return [head.ref_id, revision.ref_id]


class ExpenseCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = (
            RecordExpense.model_validate(arguments)
            if command == "docket_record_expense"
            else UpdateExpense.model_validate(arguments)
        )
        values = public.model_dump(mode="json", exclude_unset=True)
        service = ExpenseService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            head = service.head(public.expense_ref) if isinstance(public, UpdateExpense) else None
            old = service.spec(head) if head else None
            patch = {key: value for key, value in values.items() if key in ExpenseSpec.model_fields}
            if head and not patch:
                raise DocketError(
                    code="expense_change_required",
                    message="Supply a field or reported status to change.",
                )
            patch["claim_amount_supplied"] = "claim_amount" in values
            for key, default in (
                ("ownership", "unknown"),
                ("source_method", "report"),
                ("receipt_required", False),
            ):
                if key in patch and patch[key] is None:
                    patch[key] = default
            fields = old.model_dump() if old else {}
            if "receipt_ref" in patch and patch["receipt_ref"] is None:
                fields["receipt_status"] = "not_provided"
            spec = service.normalize(ExpenseSpec.model_validate({**fields, **patch}))
            retained = _named_refs(old.model_dump(mode="json")) if old else set()
            if spec.receipt_ref:
                retained.add(spec.receipt_ref)
            variant = ExpenseModify if head else ExpenseCreate
            action = variant.model_validate(
                {
                    "change_id": "expense",
                    "action": "update" if head else "create",
                    "object_type": "expense",
                    "object_ref": head.ref_id if head else None,
                    "affected_fields": sorted(patch) or ["expense"],
                    "payload" if head else "create_spec": spec,
                }
            )
            return mutation_plan(
                context, [mutation_input_json(action)], command, retained_refs=retained
            )

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

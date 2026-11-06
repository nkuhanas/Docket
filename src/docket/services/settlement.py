"""Canonical receipt, partial settlement, credit/charge and reversal operations."""

from datetime import timedelta, timezone
from fractions import Fraction
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.financial_pricing import NANO, distribute_money
from docket.domain.public_refs import new_public_ref
from docket.domain.work_time import work_zone
from docket.models import (
    AuditEvent,
    Engagement,
    Entity,
    Expense,
    FinancialEntry,
    FinancialTransaction,
    Payment,
    PaymentEvent,
    Source,
)
from docket.models.base import utc_now
from docket.schemas.settlement import (
    FinancialAdjustmentFields,
    FinancialReversalFields,
    PaymentAllocationFields,
    ReceiptSpec,
    SettlementSpec,
)
from docket.services.compensation_policies import instant
from docket.services.financial_documents import FinancialDocumentService
from docket.services.financial_state import FinancialState, amount_text, scope_rows
from docket.services.work_attribution import WorkAttributionService


class SettlementService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def owned(self, model: Any, ref: str, *, lock: bool = False) -> Any:
        stmt = select(model).where(
            model.ref_id == ref, model.operator_ref == require_principal("read").operator_ref
        )
        if lock:
            stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
        row = self.session.scalar(stmt)
        if row is None:
            raise DocketError(
                code="financial_record_unavailable",
                message="Select an owned financial record.",
                details={"field": "ref"},
            )
        return row

    def target(self, ref: str, *, lock: bool = False) -> Any:
        return (
            FinancialDocumentService(self.session).head(ref, lock=lock)
            if ref.startswith("fdoc_")
            else self.owned(Expense, ref, lock=lock)
        )

    def normalize_receipt(self, data: ReceiptSpec) -> ReceiptSpec:
        fields = data.model_dump()
        now = utc_now()
        if data.received_at is not None:
            if data.received_at.tzinfo is None or data.received_at > now:
                raise DocketError(
                    code="invalid_payment_time",
                    message="Use a past offset timestamp or leave unknown.",
                    details={"field": "received_at"},
                )
            offset = data.received_offset_seconds
            if offset is None:
                supplied_offset = data.received_at.utcoffset()
                assert supplied_offset is not None
                offset = int(supplied_offset.total_seconds())
            fields["received_offset_seconds"] = offset
            if (
                data.received_on
                and data.received_at.astimezone(timezone(timedelta(seconds=offset))).date()
                != data.received_on
            ):
                raise DocketError(
                    code="payment_date_conflict",
                    message="Use the reported receipt date matching the supplied offset timestamp.",
                    details={"field": "received_on"},
                )
            fields["received_at"] = instant(data.received_at)
        if (
            data.received_on is not None
            and data.received_on
            > now.astimezone(
                timezone(timedelta(seconds=fields["received_offset_seconds"]))
                if data.received_at
                else work_zone(get_settings().timezone)
            ).date()
        ):
            raise DocketError(
                code="invalid_payment_date",
                message="A receipt date must not be in the future.",
                details={"field": "received_on"},
            )
        if data.engagement_ref:
            WorkAttributionService(self.session).head(Engagement, data.engagement_ref)
        if data.payer_ref:
            entity = self.session.scalar(select(Entity).where(Entity.ref_id == data.payer_ref))
            if (
                entity is None
                or entity.entity_kind not in {"person", "organization", "institution"}
                or entity.canonical_status == "retracted"
            ):
                raise DocketError(
                    code="invalid_payment_payer",
                    message="Select an available payer.",
                    details={"field": "payer_ref"},
                )
        if (
            data.source_ref
            and self.session.scalar(select(Source.ref_id).where(Source.ref_id == data.source_ref))
            is None
        ):
            fields.update(
                source_ref=None, evidence_gap="Reported payment evidence was unavailable."
            )
        if data.source_method == "import" and not fields.get("source_ref"):
            fields["evidence_gap"] = (
                fields.get("evidence_gap") or "Imported receipt has no retained evidence source."
            )
        if data.target_ref:
            self.target(data.target_ref)
        if data.replaces_ref:
            parent = self.owned(Payment, data.replaces_ref)
            while parent.replaces_ref:
                parent = self.owned(Payment, parent.replaces_ref)
            if data.external_id and (data.external_source, data.external_id) != (
                parent.external_source,
                parent.external_id,
            ):
                raise DocketError(
                    code="payment_replacement_identity_conflict",
                    message="A replacement retains the original external receipt identity.",
                )
            fields.update(external_source=None, external_id=None)
        return ReceiptSpec.model_validate(fields)

    def normalize(self, spec: SettlementSpec) -> dict[str, Any]:
        if spec.command == "receipt":
            return self.normalize_receipt(ReceiptSpec.model_validate(spec.parameters)).model_dump(
                mode="json"
            )
        if spec.command == "allocation":
            data: Any = PaymentAllocationFields.model_validate(spec.parameters)
            self.owned(Payment, data.payment_ref)
            self.target(data.target_ref)
        elif spec.command in {"credit", "charge"}:
            data = FinancialAdjustmentFields.model_validate(spec.parameters)
            self.target(data.target_ref)
            if data.direction != spec.command:
                raise DocketError(
                    code="financial_adjustment_direction",
                    message="Preserve the prepared direction.",
                )
        else:
            data = FinancialReversalFields.model_validate(spec.parameters)
            self.owned(
                Payment
                if data.ref.startswith("pay_")
                else FinancialEntry
                if data.ref.startswith("fent_")
                else FinancialTransaction,
                data.ref,
            )
        return dict(data.model_dump(mode="json"))

    def external(self, data: ReceiptSpec) -> Payment | None:
        if data.replaces_ref:
            row = self.session.scalar(
                select(Payment).where(
                    Payment.operator_ref == require_principal("read").operator_ref,
                    Payment.replaces_ref == data.replaces_ref,
                )
            )
        elif data.external_id is None:
            return None
        else:
            row = self.session.scalar(
                select(Payment).where(
                    Payment.operator_ref == require_principal("read").operator_ref,
                    Payment.external_key_hash
                    == sha256_json([data.external_source, data.external_id]),
                )
            )
        if row:
            # Replacements retain the original transaction identity and cannot
            # fork into two active receipts for the same externally observed cash.
            while replacement := self.session.scalar(
                select(Payment).where(Payment.replaces_ref == row.ref_id)
            ):
                row = replacement
        return row

    def entry(
        self,
        txn_ref: str,
        kind: str,
        amount: Fraction,
        currency: str,
        *,
        payment_ref: str | None = None,
        target_ref: str | None = None,
        target_basis_ref: str | None = None,
        scopes: list[Any] | None = None,
        note: str | None = None,
        deduction_kind: str | None = None,
        reverses_ref: str | None = None,
    ) -> FinancialEntry:
        return FinancialEntry(
            ref_id=new_public_ref("fent"),
            transaction_ref=txn_ref,
            operator_ref=require_principal("commit").operator_ref,
            kind=kind,
            amount=amount_text(amount),
            currency=currency,
            payment_ref=payment_ref,
            target_ref=target_ref,
            target_basis_ref=target_basis_ref,
            scopes_json=scope_rows(scopes or []),
            matches_json=[],
            note=note,
            deduction_kind=deduction_kind,
            reverses_ref=reverses_ref,
        )

    def reverse_entry(
        self, txn_ref: str, entry: FinancialEntry, amount: Fraction, note: str
    ) -> FinancialEntry:
        return self.entry(
            txn_ref,
            "reversal",
            amount,
            entry.currency,
            payment_ref=entry.payment_ref,
            target_ref=entry.target_ref,
            target_basis_ref=entry.target_basis_ref,
            scopes=FinancialState.scopes(entry),
            note=note,
            reverses_ref=entry.ref_id,
        )

    def release_excess(
        self,
        txn_ref: str,
        pending: list[FinancialEntry],
        *,
        payment_ref: str | None = None,
        source_keys: set[str] | None = None,
    ) -> FinancialState:
        state = FinancialState(self.session, extra=pending)
        if source_keys is not None:
            source_keys = set(source_keys)
            prior = -1
            while prior != len(source_keys):
                prior = len(source_keys)
                for entry in state.entries:
                    keys = {scope.source_key for scope in state.scopes(entry)}
                    if keys & source_keys:
                        source_keys |= keys
        for entry in state.entries:
            if entry.kind != "cash" or (payment_ref and entry.payment_ref != payment_ref):
                continue
            if source_keys is not None and not (
                {scope.source_key for scope in state.scopes(entry)} & source_keys
            ):
                continue
            unused = state.match.unmatched.get(entry.ref_id, Fraction(0))
            if unused:
                pending.append(
                    self.reverse_entry(
                        txn_ref,
                        entry,
                        unused,
                        "Derived release of cash exceeding its retained claim scope.",
                    )
                )
        return FinancialState(self.session, extra=pending)

    def allocate(
        self,
        txn_ref: str,
        payment: Payment,
        target: str,
        amount: Fraction | None,
        deduction: Fraction,
        kind: str | None,
        note: str | None,
        pending: list[FinancialEntry],
        *,
        optional: bool = False,
    ) -> None:
        state = self.release_excess(txn_ref, pending, payment_ref=payment.ref_id)
        scopes = state.sources.scopes(target, payment.currency)
        if not scopes:
            if optional:
                return
            raise DocketError(
                code="financial_claim_not_payable",
                message="Use a posted claim with matching currency, "
                "or retain the receipt unapplied.",
                details={"field": "target_ref"},
            )
        target_data = state.sources.targets[target]
        if deduction:
            before_deduction = state.matched_total("deduction")
            row = self.entry(
                txn_ref,
                "deduction",
                deduction,
                payment.currency,
                payment_ref=payment.ref_id,
                target_ref=target,
                target_basis_ref=target_data.get("observation_ref") or target_data["revision_ref"],
                scopes=scopes,
                note=note,
                deduction_kind=kind,
            )
            pending.append(row)
            state = FinancialState(self.session, extra=pending)
            if state.matched_total("deduction") - before_deduction != deduction:
                raise DocketError(
                    code="deduction_exceeds_claim",
                    message="The explicit deduction exceeds the remaining gross claim.",
                    details={"field": "deduction_amount"},
                )
            state = self.release_excess(
                txn_ref, pending, source_keys={scope.source_key for scope in scopes}
            )
        available = state.available(payment.ref_id)
        proposed = available if amount is None else amount
        if proposed > available:
            raise DocketError(
                code="payment_amount_unavailable",
                message="Use the unallocated received amount.",
                details={"field": "amount", "available": amount_text(available)},
            )
        if proposed <= 0:
            return
        before_cash = state.matched_total("cash")
        row = self.entry(
            txn_ref,
            "cash",
            proposed,
            payment.currency,
            payment_ref=payment.ref_id,
            target_ref=target,
            target_basis_ref=target_data.get("observation_ref") or target_data["revision_ref"],
            scopes=scopes,
            note=note,
        )
        pending.append(row)
        state = FinancialState(self.session, extra=pending)
        applied = state.matched_total("cash") - before_cash
        if amount is not None and applied != amount:
            raise DocketError(
                code="payment_exceeds_claim",
                message="The requested cash exceeds this shared claim balance.",
                details={"field": "amount", "available": amount_text(applied)},
            )
        if not applied:
            pending.remove(row)
        elif applied != proposed:
            row.amount = amount_text(applied)
        state = FinancialState(self.session, extra=pending)
        for entry in pending:
            if entry.kind in {"cash", "deduction"}:
                entry.matches_json = state.matches_json(entry.ref_id)

    def receipt(
        self, txn_ref: str, data: ReceiptSpec, changeset: Any, pending: list[FinancialEntry]
    ) -> Payment:
        if data.replaces_ref:
            old = self.owned(Payment, data.replaces_ref, lock=True)
            state = FinancialState(self.session)
            if state.received[old.ref_id] or self.session.scalar(
                select(Payment.ref_id).where(Payment.replaces_ref == old.ref_id)
            ):
                raise DocketError(
                    code="payment_replacement_not_ready",
                    message="Fully reverse the prior receipt before replacing it.",
                )
        receipt = Payment(
            ref_id=new_public_ref("pay"),
            operator_ref=require_principal("commit").operator_ref,
            version=1,
            amount=amount_text(Fraction(data.amount)),
            currency=data.currency,
            received_at=data.received_at,
            received_offset_seconds=data.received_offset_seconds,
            received_on=data.received_on,
            engagement_ref=data.engagement_ref,
            payer_ref=data.payer_ref,
            description=data.description,
            category=data.category,
            source_method=data.source_method,
            source_ref=data.source_ref,
            evidence_gap=data.evidence_gap,
            external_source=data.external_source,
            external_id=data.external_id,
            external_key_hash=sha256_json([data.external_source, data.external_id])
            if data.external_id
            else None,
            replaces_ref=data.replaces_ref,
            basis_refs=list(changeset.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(receipt)
        self.session.flush()
        pending.append(
            self.entry(
                txn_ref, "receipt", Fraction(data.amount), data.currency, payment_ref=receipt.ref_id
            )
        )
        if data.target_ref:
            self.allocate(
                txn_ref,
                receipt,
                data.target_ref,
                None,
                Fraction(data.deduction_amount or 0),
                data.deduction_kind,
                None,
                pending,
                optional=True,
            )
        return receipt

    def adjust(
        self, txn_ref: str, data: FinancialAdjustmentFields, pending: list[FinancialEntry]
    ) -> None:
        state = FinancialState(self.session, extra=pending)
        scopes = state.sources.scopes(data.target_ref, data.currency)
        currencies = {scope.currency for scope in scopes}
        if len(currencies) != 1:
            raise DocketError(
                code="financial_claim_currency_required",
                message="Choose one established claim currency.",
                details={"field": "currency"},
            )
        currency = next(iter(currencies))
        target = state.sources.targets[data.target_ref]
        row = self.entry(
            txn_ref,
            data.direction,
            Fraction(data.amount),
            currency,
            target_ref=data.target_ref,
            target_basis_ref=target.get("observation_ref") or target["revision_ref"],
            scopes=scopes,
            note=data.reason,
        )
        if data.direction == "charge":
            parts = state.target_parts(data.target_ref, currency)
            if not parts:
                raise DocketError(
                    code="financial_claim_not_payable", message="Select a posted claim."
                )
            values = [part.amount for part in parts]
            if not any(values):
                values = [Fraction(part.end - part.start) for part in parts]
            units = distribute_money(int(Fraction(data.amount) * NANO), values)
            row.matches_json = [
                {
                    "source_key": part.source_key,
                    "start": part.start,
                    "end": part.end,
                    "currency": currency,
                    "amount_nano": unit,
                    "basis_ref": state.primary[part.ref].basis_ref,
                }
                for part, unit in zip(parts, units, strict=True)
            ]
        pending.append(row)
        if data.direction == "credit":
            result = FinancialState(self.session, extra=pending)
            if result.matched_total("credit") - state.matched_total("credit") != Fraction(
                data.amount
            ):
                raise DocketError(
                    code="credit_exceeds_claim",
                    message="This credit exceeds the remaining shared gross claim.",
                    details={"field": "amount"},
                )
            row.matches_json = result.matches_json(row.ref_id)
            self.release_excess(
                txn_ref, pending, source_keys={scope.source_key for scope in scopes}
            )

    def reverse(
        self, txn_ref: str, data: FinancialReversalFields, pending: list[FinancialEntry]
    ) -> None:
        state = FinancialState(self.session)
        rows = [
            row
            for row in state.entries
            if row.kind != "reversal"
            and (
                row.ref_id == data.ref
                or row.transaction_ref == data.ref
                or (row.kind == "receipt" and row.payment_ref == data.ref)
            )
        ]
        if not rows:
            raise DocketError(
                code="financial_record_not_reversible",
                message="Select a retained receipt or posting.",
            )
        if any(row.kind == "receipt" for row in rows):
            rows = [row for row in rows if row.kind == "receipt"]
        currencies = {row.currency for row in rows}
        if len(currencies) != 1:
            raise DocketError(
                code="financial_reversal_currency_conflict", message="Reverse a specific entry."
            )
        available = sum((state.effective[row.ref_id] for row in rows), Fraction(0))
        remaining = Fraction(data.amount) if data.amount is not None else available
        if remaining <= 0 or remaining > available:
            raise DocketError(
                code="financial_reversal_exceeds_record",
                message="Use the retained unreversed amount.",
                details={"field": "amount", "available": amount_text(available)},
            )
        for row in sorted(rows, key=lambda r: r.ref_id):
            amount = min(remaining, state.effective[row.ref_id])
            if not amount:
                continue
            if row.kind == "receipt" and row.payment_ref:
                payment_ref = row.payment_ref
                after = state.received[payment_ref] - amount
                assigned = state.cash_active(payment_ref)
                release = max(Fraction(0), assigned - after)
                for cash in reversed(state.entries):
                    if cash.kind == "cash" and cash.payment_ref == payment_ref and release:
                        take = min(release, state.effective[cash.ref_id])
                        if take:
                            pending.append(self.reverse_entry(txn_ref, cash, take, data.reason))
                            release -= take
            pending.append(self.reverse_entry(txn_ref, row, amount, data.reason))
            remaining -= amount
        self.release_excess(
            txn_ref,
            pending,
            source_keys={
                scope.source_key for entry in pending for scope in FinancialState.scopes(entry)
            },
        )

    def apply(self, session: Session, changeset: Any, change: Any) -> list[str]:
        principal = require_principal("commit")
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "financial-ledger:" + principal.operator_ref},
            )
        spec = SettlementSpec.model_validate(change.create_spec)
        parameters = self.normalize(spec)
        if spec.command == "receipt":
            duplicate = self.external(ReceiptSpec.model_validate(parameters))
            if duplicate:
                if (
                    duplicate.amount != amount_text(Fraction(str(parameters["amount"])))
                    or duplicate.currency != parameters["currency"]
                ):
                    raise DocketError(
                        code="payment_external_identity_conflict",
                        message="This external receipt already exists with different money.",
                        details={"payment_ref": duplicate.ref_id},
                    )
                original = self.session.scalar(
                    select(FinancialTransaction).where(
                        FinancialTransaction.payment_ref == duplicate.ref_id,
                        FinancialTransaction.kind == "receipt",
                    )
                )
                assert original is not None
                if (
                    parameters.get("target_ref")
                    and original.parameters_json.get("target_ref") != parameters["target_ref"]
                ):
                    raise DocketError(
                        code="payment_already_recorded",
                        message="Apply the existing receipt to the selected claim.",
                        details={
                            "payment_ref": duplicate.ref_id,
                            "next_action": "docket_apply_payment",
                        },
                    )
                self.audit(original, changeset, change, [duplicate.ref_id], deduplicated=True)
                return [original.ref_id, duplicate.ref_id]
        for name in ("payment_ref", "replaces_ref", "target_ref", "ref"):
            ref = parameters.get(name)
            if not isinstance(ref, str) or not ref.startswith(("pay_", "fdoc_", "exp_")):
                continue
            head = (
                self.owned(Payment, ref, lock=True)
                if ref.startswith("pay_")
                else self.target(ref, lock=True)
            )
            if changeset.expected_versions.get(ref) != head.version:
                raise DocketError(
                    code="financial_version_conflict",
                    message="Read current financial context and retry.",
                    details={"ref": ref, "next_action": "docket_get_financial_context"},
                )
        state = FinancialState(self.session)
        if state.fingerprint(parameters) != spec.basis_hash:
            raise DocketError(
                code="financial_basis_changed",
                message="Financial state changed. Prepare from current records.",
            )
        txn_ref = new_public_ref("ftxn")
        pending: list[FinancialEntry] = []
        payment = None
        if spec.command == "receipt":
            payment = self.receipt(
                txn_ref, ReceiptSpec.model_validate(parameters), changeset, pending
            )
        elif spec.command == "allocation":
            data = PaymentAllocationFields.model_validate(parameters)
            payment = self.owned(Payment, data.payment_ref, lock=True)
            self.allocate(
                txn_ref,
                payment,
                data.target_ref,
                Fraction(data.amount) if data.amount is not None else None,
                Fraction(data.deduction_amount or 0),
                data.deduction_kind,
                data.note,
                pending,
            )
        elif spec.command in {"credit", "charge"}:
            self.adjust(txn_ref, FinancialAdjustmentFields.model_validate(parameters), pending)
        else:
            self.reverse(txn_ref, FinancialReversalFields.model_validate(parameters), pending)
        if not pending:
            raise DocketError(
                code="financial_no_new_posting",
                message="There is no available amount to post; existing records remain unchanged.",
            )
        final = FinancialState(self.session, extra=pending)
        touched = {row.payment_ref for row in pending if row.payment_ref}
        for ref in sorted(touched):
            head = self.owned(Payment, ref, lock=True)
            if head is not payment or spec.command != "receipt":
                head.version += 1
                head.updated_at = utc_now()
        self.session.flush()
        outcome = {
            "ref": txn_ref,
            "kind": spec.command,
            "entries": [row.ref_id for row in pending],
            "payments": [
                {
                    "ref": ref,
                    "version": self.owned(Payment, ref).version,
                    "received": amount_text(final.received[ref]),
                    "unapplied": amount_text(final.available(ref)),
                    "currency": self.owned(Payment, ref).currency,
                }
                for ref in sorted(touched)
            ],
            "targets": sorted({row.target_ref for row in pending if row.target_ref}),
            "provider_effects": False,
        }
        txn = FinancialTransaction(
            ref_id=txn_ref,
            operator_ref=principal.operator_ref,
            kind=spec.command,
            payment_ref=payment.ref_id if payment else None,
            payment_version=payment.version if payment else None,
            parameters_json=parameters,
            outcome_json=outcome,
            basis_refs=list(change.basis_refs),
            created_by_changeset_ref=changeset.ref_id,
        )
        self.session.add(txn)
        self.session.flush()
        for entry in pending:
            self.session.add(entry)
            self.session.flush()
        for ref in sorted(touched):
            self.session.add(
                PaymentEvent(
                    payment_ref=ref,
                    version=self.owned(Payment, ref).version,
                    transaction_ref=txn_ref,
                )
            )
        self.session.flush()
        refs = [txn_ref, *sorted(touched), *(row.ref_id for row in pending)]
        self.audit(txn, changeset, change, refs)
        return refs

    def audit(
        self,
        txn: FinancialTransaction,
        changeset: Any,
        change: Any,
        refs: list[str],
        *,
        deduplicated: bool = False,
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type="financial_transaction." + txn.kind,
                entity_type="financial_transaction",
                entity_id=txn.id,
                actor_type="operator",
                actor_id=require_principal("commit").operator_ref,
                primary_ref=txn.ref_id,
                affected_refs=refs,
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "transaction_ref": txn.ref_id,
                    "kind": txn.kind,
                    "deduplicated": deduplicated,
                },
            )
        )

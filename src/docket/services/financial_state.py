"""Exact current/as-of bookkeeping views over immutable claims and ledger entries."""

from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.financial_pricing import NANO, distribute_money
from docket.domain.settlement_flow import SettlementIntent, SettlementPart, SettlementScope, settle
from docket.models import FinancialEntry, Payment
from docket.models.base import utc_now
from docket.services.financial_sources import FinancialSourceService


def fraction(value: str) -> Fraction:
    return Fraction(Decimal(value))


def amount_text(value: Fraction) -> str:
    nano = value * NANO
    if nano.denominator != 1:
        raise ValueError("Posting requires exact nine-place currency units")
    whole, remainder = divmod(abs(nano.numerator), NANO)
    text = str(whole) + (f".{remainder:09}".rstrip("0") if remainder else "")
    return ("-" if value < 0 else "") + text


def scope_rows(scopes: list[SettlementScope]) -> list[dict[str, Any]]:
    return [asdict(scope) for scope in scopes]


class FinancialState:
    def __init__(
        self, session: Session, *, as_of: Any = None, extra: list[FinancialEntry] | None = None
    ) -> None:
        self.session = session
        self.as_of = as_of or utc_now()
        owner = require_principal("read").operator_ref
        self.sources = FinancialSourceService(session).capture(self.as_of)
        self.payments = {
            row.ref_id: row
            for row in session.scalars(
                select(Payment).where(
                    Payment.operator_ref == owner, Payment.created_at <= self.as_of
                )
            )
        }
        self.entries = list(
            session.scalars(
                select(FinancialEntry)
                .where(
                    FinancialEntry.operator_ref == owner, FinancialEntry.created_at <= self.as_of
                )
                .order_by(FinancialEntry.ref_id)
            )
        )
        self.entries.extend(extra or [])
        self.by_ref = {row.ref_id: row for row in self.entries}
        reversed_amounts: dict[str, Fraction] = {}
        for entry in self.entries:
            if entry.reverses_ref:
                reversed_amounts[entry.reverses_ref] = reversed_amounts.get(
                    entry.reverses_ref, Fraction(0)
                ) + fraction(entry.amount)
        self.effective = {
            row.ref_id: fraction(row.amount) - reversed_amounts.get(row.ref_id, Fraction(0))
            for row in self.entries
            if row.kind != "reversal"
        }
        if any(value < 0 for value in self.effective.values()):
            raise ValueError("Financial reversal exceeds its retained entry")
        self.received = {ref: Fraction(0) for ref in self.payments}
        scopes = []
        for entry in self.entries:
            if entry.kind == "receipt" and entry.payment_ref:
                self.received[entry.payment_ref] += self.effective[entry.ref_id]
            if entry.kind != "reversal" and self.effective[entry.ref_id] > 0:
                scopes.extend(self.scopes(entry))
        self.parts, self.primary = self.sources.parts(scopes)
        self.add_charges()
        self.intents = []
        for row in self.entries:
            if row.kind in {"cash", "deduction", "credit"} and self.effective[row.ref_id] > 0:
                self.intents.append(
                    SettlementIntent(
                        row.ref_id,
                        row.kind,
                        self.effective[row.ref_id],
                        row.currency,
                        tuple(self.scopes(row)),
                        row.payment_ref,
                    )
                )
        self.match = settle(self.parts, self.intents, self.received)

    @staticmethod
    def scopes(entry: FinancialEntry) -> list[SettlementScope]:
        return [SettlementScope(**item) for item in entry.scopes_json]

    def add_charges(self) -> None:
        charged = {part.ref: part.amount for part in self.parts}
        for entry in self.entries:
            if entry.kind != "charge" or self.effective[entry.ref_id] <= 0:
                continue
            units = distribute_money(
                int(self.effective[entry.ref_id] * NANO),
                [Fraction(int(match["amount_nano"])) for match in entry.matches_json],
            )
            for match, unit in zip(entry.matches_json, units, strict=True):
                first, last = int(match["start"]), int(match["end"])
                value = Fraction(unit, NANO)
                for part in self.parts:
                    if part.source_key != match["source_key"] or part.currency != entry.currency:
                        continue
                    lo, hi = max(first, part.start), min(last, part.end)
                    if hi > lo:
                        # Charge entries retain monetary units over their chosen
                        # source range. Use cumulative allocation at one nanounit.
                        def cumulative(
                            point: int,
                            amount: Fraction = value,
                            start: int = first,
                            end: int = last,
                        ) -> int:
                            from docket.domain.compensation import rounded_integer

                            return rounded_integer(
                                amount * NANO * Fraction(point - start, end - start)
                            )

                        charged[part.ref] += Fraction(cumulative(hi) - cumulative(lo), NANO)
        self.parts = [
            SettlementPart(p.ref, p.source_key, p.start, p.end, p.currency, charged[p.ref])
            for p in self.parts
        ]

    def matched_total(self, kind: str) -> Fraction:
        return sum(
            (self.matched(row.ref_id) for row in self.entries if row.kind == kind),
            Fraction(0),
        )

    def cash_active(self, payment_ref: str) -> Fraction:
        return sum(
            (
                self.effective[row.ref_id]
                for row in self.entries
                if row.kind == "cash" and row.payment_ref == payment_ref
            ),
            Fraction(0),
        )

    def available(self, payment_ref: str) -> Fraction:
        return self.received[payment_ref] - self.cash_active(payment_ref)

    def matched(self, ref: str) -> Fraction:
        return sum(self.match.matches.get(ref, {}).values(), Fraction(0))

    def target_parts(self, target_ref: str, currency: str | None = None) -> list[SettlementPart]:
        scopes = self.sources.scopes(target_ref, currency)
        return [part for part in self.parts if any(scope.covers(part) for scope in scopes)]

    def fingerprint(self, parameters: dict[str, Any]) -> str:
        keys: set[str] = set()
        payment_refs: set[str] = set()
        target = parameters.get("target_ref")
        if target:
            keys.update(scope.source_key for scope in self.sources.scopes(target))
        if parameters.get("payment_ref"):
            payment_refs.add(parameters["payment_ref"])
        if parameters.get("replaces_ref"):
            payment_refs.add(parameters["replaces_ref"])
        ref = parameters.get("ref")
        for entry in self.entries:
            if ref and ref in {entry.ref_id, entry.transaction_ref, entry.payment_ref}:
                keys.update(scope.source_key for scope in self.scopes(entry))
                if entry.payment_ref:
                    payment_refs.add(entry.payment_ref)
        # A receipt/whole-document allocation can connect several source ranges.
        # Close only that affected component; unrelated financial work stays out.
        changed = True
        relevant = []
        while changed:
            changed = False
            for row in self.entries:
                row_keys = {scope.source_key for scope in self.scopes(row)}
                if row_keys & keys or row.payment_ref in payment_refs:
                    before = (len(keys), len(payment_refs))
                    keys |= row_keys
                    if row.payment_ref:
                        payment_refs.add(row.payment_ref)
                    changed |= before != (len(keys), len(payment_refs))
        for row in self.entries:
            if (
                ({scope.source_key for scope in self.scopes(row)} & keys)
                or row.payment_ref in payment_refs
                or ref in {row.ref_id, row.transaction_ref}
            ):
                relevant.append(row.ref_id)
        quotes = [
            (q.ref, q.basis_ref, q.amount_nano, q.start, q.end, q.currency)
            for q in self.sources.quotes
            if q.source_key in keys
        ]
        return sha256_json(
            {
                "payments": sorted(
                    (ref, self.payments[ref].version)
                    for ref in payment_refs
                    if ref in self.payments
                ),
                "entries": sorted(relevant),
                "quotes": sorted(quotes),
                "target": self.sources.targets.get(target) if target else None,
            }
        )

    def matches_json(self, entry_ref: str) -> list[dict[str, Any]]:
        parts = {part.ref: part for part in self.parts}
        return [
            {
                "source_key": parts[ref].source_key,
                "start": parts[ref].start,
                "end": parts[ref].end,
                "currency": parts[ref].currency,
                "amount_nano": int(amount * NANO),
                "basis_ref": self.primary[ref].basis_ref,
            }
            for ref, amount in sorted(self.match.matches.get(entry_ref, {}).items())
        ]

"""Shared financial evidence admission, extraction, and canonical adoption."""

import hashlib
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.public_refs import new_public_ref
from docket.models import AttachmentEvidence, AuthenticatedRequest, Source
from docket.models.base import utc_now
from docket.models.financial_intake import FinancialCandidate, FinancialCollector, FinancialIntake
from docket.models.funding import AccountActivity
from docket.schemas.authority import ExpenseCreate
from docket.schemas.expenses import ExpenseSpec
from docket.schemas.financial_intake import ResolveEvidence
from docket.services.attachment_evidence import AttachmentCapture
from docket.services.expenses import ExpenseService
from docket.services.financial_files import extract, media_type, purchase_key, rejected
from docket.services.financial_inference import suggest
from docket.services.funding import FundingService, attachment_store


class FinancialIntakeService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def find(self, owner: str, origin: str, delivery: str) -> FinancialIntake | None:
        return self.session.scalar(
            select(FinancialIntake).where(
                FinancialIntake.operator_ref == owner,
                FinancialIntake.origin_key == origin,
                FinancialIntake.delivery_key == delivery,
            )
        )

    def admit(
        self,
        data: bytes,
        *,
        delivery: str,
        input_hash: str,
        declared: str | None,
        filename: str | None,
        declarations: dict[str, Any],
        request: AuthenticatedRequest | None = None,
        collector: FinancialCollector | None = None,
    ) -> FinancialIntake:
        if not data or len(data) > min(
            self.settings.attachment_max_bytes, self.settings.attachment_total_max_bytes
        ):
            raise rejected("invalid_file_size")
        if collector and collector.enabled and request is None:
            owner, origin = collector.operator_ref, collector.ref_id
        elif request and collector is None:
            owner, origin = request.operator_ref, request.principal_ref
            if owner != require_principal("commit").operator_ref:
                raise rejected("financial_source_unavailable")
        else:
            raise rejected("financial_intake_not_authorized")
        binding = sha256_json([owner, origin, delivery])
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(binding[:15], 16)}
            )
        digest = hashlib.sha256(data).hexdigest()
        prior = self.find(owner, origin, delivery)
        if prior:
            if prior.digest != digest or prior.input_hash != input_hash:
                raise rejected("financial_delivery_conflict")
            return prior
        media = media_type(data, declared, filename)
        src = new_public_ref("src")
        now = utc_now()
        store = attachment_store(self.session, self.settings)
        if request:
            capture = AttachmentCapture(
                transport_attachment_ref=binding,
                filename=None,
                media_type=media,
                byte_size=len(data),
                received_at=now,
                plaintext=data,
            )
            (src,) = store.plan_request(request, [capture])
        else:
            source = Source(
                ref_id=src,
                source_kind="attachment",
                external_ref="financial:" + binding,
                observed_at=now,
                content_hash=digest,
                metadata_json={"financial_intake": True},
            )
            self.session.add(source)
            self.session.flush()
            self.session.add(
                AttachmentEvidence(
                    ref_id=src,
                    transport="financial_collector" if collector else "agent_reported",
                    transport_attachment_ref=binding,
                    source_message_ref=delivery,
                    authenticated_request_ref=request.ref_id if request else None,
                    collector_ref=collector.ref_id if collector else None,
                    media_type=media,
                    byte_size=len(data),
                    content_hash=digest,
                    received_at=now,
                    ingest_state="available",
                    retention_disposition="retained_encrypted",
                )
            )
            self.session.flush()
            self.session.add(store.encrypted_blob(src, data))
        row = FinancialIntake(
            source_ref=src,
            operator_ref=owner,
            origin_key=origin,
            delivery_key=delivery,
            input_hash=input_hash,
            digest=digest,
            request_ref=request.ref_id if request else None,
            collector_ref=collector.ref_id if collector else None,
            policy_snapshot={**collector.policy, "revision": collector.revision}
            if collector
            else {},
            declarations=declarations,
            media_type=media,
            byte_count=len(data),
            status="pending",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def process(self, source: str | None = None) -> FinancialIntake | None:
        stmt = select(FinancialIntake).where(FinancialIntake.status == "pending")
        if source:
            stmt = stmt.where(FinancialIntake.source_ref == source)
        row = self.session.scalar(
            stmt.order_by(FinancialIntake.created_at).limit(1).with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        if row.declarations.get("evidence_role") in ("income_document", "statement"):
            row.status, row.reason = "complete", "original_retained_for_review"
            return row
        # Storage faults roll back for retry. Extraction failures preserve the source.
        raw = attachment_store(self.session, self.settings).plaintext(row.source_ref)
        try:
            extracted = extract(raw, row.media_type)
        except Exception:
            row.status, row.reason = "failed", "unreadable_original"
            extracted = []
        else:
            row.status = "complete" if extracted else "partial"
            row.reason = None if extracted else "financial_facts_unresolved"
        for index, facts in enumerate(extracted or [{"anchor": "file"}]):
            key = purchase_key(facts)
            enough = not facts.get("review_only") and all(
                facts.get(k) for k in ("vendor", "amount", "currency")
            )
            if not enough and row.status == "complete":
                row.status, row.reason = "partial", "financial_facts_unresolved"
            self.session.add(
                FinancialCandidate(
                    source_ref=row.source_ref,
                    ordinal=index,
                    facts=facts,
                    purchase_key=key,
                    disposition="candidate_created" if enough else "evidence_only",
                    review_reason=("adoption_pending" if enough else "expense_facts_unresolved"),
                )
            )
        self.session.flush()
        return row

    def suggest_pending(self) -> None:
        if not (
            self.settings.financial_classifier_model or self.settings.financial_extraction_model
        ):
            return
        row = self.session.scalar(
            select(FinancialIntake)
            .where(
                FinancialIntake.status == "partial",
                FinancialIntake.inference_attempts < 3,
                (
                    FinancialIntake.next_attempt.is_(None)
                    | (FinancialIntake.next_attempt <= utc_now())
                ),
            )
            .order_by(FinancialIntake.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return
        raw = attachment_store(self.session, self.settings).plaintext(row.source_ref)
        row.inference_attempts += 1
        try:
            row.inference = suggest(raw, row.media_type, self.settings)
        except Exception:
            row.inference = {"status": "inference_unavailable", "attempts": row.inference_attempts}
            row.next_attempt = utc_now() + timedelta(minutes=2**row.inference_attempts)
        else:
            row.next_attempt = None
            row.inference_attempts = 3

    def owned(self, ref: str) -> FinancialIntake:
        row = self.session.get(FinancialIntake, ref)
        if row is None or row.operator_ref != require_principal("read").operator_ref:
            raise rejected("financial_source_unavailable")
        return row

    def candidates(self, source: str) -> list[FinancialCandidate]:
        return list(
            self.session.scalars(
                select(FinancialCandidate)
                .where(FinancialCandidate.source_ref == source)
                .order_by(FinancialCandidate.ordinal)
            )
        )

    @staticmethod
    def declaration(row: FinancialIntake, candidate: FinancialCandidate) -> dict[str, Any]:
        if row.collector_ref and candidate.disposition != "evidence_only":
            return {
                **row.policy_snapshot["declaration"],
                "origin": "standing_policy",
                "policy_revision": row.policy_snapshot["revision"],
            }
        return row.declarations

    def related_evidence(self, expense: str, owner: str) -> dict[str, Any]:
        rows = self.session.execute(
            select(FinancialCandidate, FinancialIntake)
            .join(FinancialIntake)
            .where(FinancialCandidate.expense_ref == expense, FinancialIntake.operator_ref == owner)
            .order_by(FinancialIntake.created_at, FinancialCandidate.ordinal)
            .limit(21)
        ).all()
        declarations = [self.declaration(i, c) for c, i in rows]
        conflict = any(
            len(
                {
                    str(Decimal(str(d[k])).normalize())
                    if k == "business_use_percent"
                    else str(d[k])
                    for d in declarations
                    if k in d
                }
            )
            > 1
            for k in ("business_use_percent", "write_off_intent")
        )
        return {
            "evidence": [
                {
                    "source_ref": i.source_ref,
                    "candidate_ref": c.ref_id,
                    "activity_ref": c.activity_ref,
                    "instrument_revision_ref": c.instrument_revision_ref,
                    "declarations": self.declaration(i, c),
                }
                for c, i in rows[:20]
            ],
            "more_evidence": len(rows) > 20,
            "conflicting_declarations": conflict,
        }

    def receipt(self, row: FinancialIntake, offset: int = 0, limit: int = 8) -> dict[str, Any]:
        candidates = self.candidates(row.source_ref)
        outcomes = []
        for c in candidates[offset : offset + limit]:
            declaration = self.declaration(row, c)
            conflict = bool(
                c.expense_ref
                and self.related_evidence(c.expense_ref, row.operator_ref)[
                    "conflicting_declarations"
                ]
            )
            outcomes.append(
                {
                    "candidate_ref": c.ref_id,
                    "version": c.version,
                    "facts": c.facts,
                    "disposition": c.disposition,
                    "expense_ref": c.expense_ref,
                    "expense_revision_ref": c.expense_revision_ref,
                    "declarations": declaration,
                    "funding": c.instrument_revision_ref or "unresolved",
                    "activity_ref": c.activity_ref,
                    "tax_review": "unreviewed",
                    "conflicting_declarations": conflict,
                    "review_required": bool(c.review_reason or conflict),
                    "review_reason": c.review_reason,
                }
            )
        return {
            "ok": True,
            "source_ref": row.source_ref,
            "source_accepted": True,
            "original_sha256": row.digest,
            "byte_count": row.byte_count,
            "extraction": row.status,
            "reason": row.reason,
            "reported_declarations": row.declarations,
            "optional_inference": row.inference,
            "outcomes": outcomes,
            "next_cursor": str(offset + limit) if len(candidates) > offset + limit else None,
            "outcome_count": len(outcomes),
            "review_required": bool(
                row.reason
                or any(c.review_reason for c in candidates)
                or any(o["review_required"] for o in outcomes)
            ),
        }

    def resolve(self, session: Session, changeset: Any, change: Any) -> list[str]:
        from docket.services.financial_review import current_facts, legacy_matches, lock_review

        owner = require_principal("commit").operator_ref
        lock_review(session, owner)
        candidate = session.scalar(
            select(FinancialCandidate)
            .where(FinancialCandidate.ref_id == change.object_ref)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            raise rejected("candidate_unavailable")
        src = self.owned(candidate.source_ref)
        if changeset.expected_versions.get(candidate.ref_id) != candidate.version:
            raise rejected("candidate_version_conflict")
        choice = ResolveEvidence.model_validate(change.payload)
        expenses = ExpenseService(session)
        facts = candidate.facts
        effect = candidate.disposition
        ref = candidate.expense_ref
        if candidate.activity_ref and choice.instrument_ref:
            linked_activity = session.get(AccountActivity, candidate.activity_ref)
            if linked_activity and linked_activity.fields.get("instrument_ref") not in (
                None,
                choice.instrument_ref,
            ):
                raise rejected("activity_instrument_conflict")
        if ref and choice.existing_expense_ref and choice.existing_expense_ref != ref:
            raise rejected("expense_link_conflict")
        key = candidate.purchase_key
        if key:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)}
                )
            known = session.scalar(
                select(FinancialCandidate.expense_ref)
                .join(FinancialIntake)
                .where(
                    FinancialIntake.operator_ref == src.operator_ref,
                    FinancialCandidate.purchase_key == key,
                    FinancialCandidate.expense_ref.is_not(None),
                )
                .limit(1)
            )
            if known and choice.existing_expense_ref and choice.existing_expense_ref != known:
                raise rejected("expense_identity_conflict")
        else:
            known = None
        linked = choice.existing_expense_ref or known
        if not ref and linked:
            expense = expenses.revision(expenses.head(linked, lock=True))
            if facts.get("currency", expense.currency) != expense.currency or (
                "amount" in facts
                and str(expense.amount) != facts["amount"]
                and Decimal(str(expense.amount)) != Decimal(facts["amount"])
            ):
                raise rejected("expense_amount_conflict")
            ref, effect = linked, "existing_expense_linked"
        if not ref:
            if facts.get("review_only"):
                raise rejected("expense_requires_independent_evidence")
            if not all(facts.get(k) for k in ("amount", "currency", "vendor")):
                raise rejected("insufficient_expense_evidence")
            spec = ExpenseSpec.model_validate(
                {
                    "amount": facts["amount"],
                    "currency": facts["currency"],
                    "description": facts["vendor"] + " purchase",
                    "vendor": facts["vendor"],
                    "incurred_on": facts.get("incurred_on"),
                    "source_method": "import",
                    "receipt_ref": src.source_ref,
                    "external_source": "financial_intake",
                    "external_id": key or candidate.ref_id,
                }
            )
            mutation = ExpenseCreate(
                change_id="expense",
                action="create",
                object_type="expense",
                affected_fields=["amount", "receipt_ref"],
                create_spec=spec,
                basis_refs=change.basis_refs,
            )
            ref = expenses.apply(session, changeset, mutation)[0]
            effect = "new_expense_recorded"
        pinned_instrument = None
        if choice.activity_ref:
            activity = session.scalar(
                select(AccountActivity)
                .where(
                    AccountActivity.ref_id == choice.activity_ref,
                    AccountActivity.operator_ref == src.operator_ref,
                )
                .with_for_update()
            )
            if activity is None:
                raise rejected("activity_unavailable")
            if any(
                r.kind in ("activity_match", "tax_allocation")
                and (
                    activity.ref_id
                    in (r.fields.get("activity_ref"), r.fields.get("counterpart_ref"))
                    or (r.fields.get("expense_ref") == ref and r.fields.get("role") == "purchase")
                )
                for r in current_facts(session, owner)
            ) or legacy_matches(session, owner).get(ref, set()) - {activity.ref_id}:
                raise rejected("activity_link_conflict")
            posting = activity.fields
            cost = expenses.revision(expenses.head(ref, lock=True))
            if (
                posting["state"] != "posted"
                or posting["kind"] not in ("purchase", "unknown")
                or Decimal(posting["amount"]) != -Decimal(str(cost.amount))
                or posting["currency"] != cost.currency
            ):
                raise rejected("activity_purchase_mismatch")
            conflicting = session.scalar(
                select(FinancialCandidate.ref_id)
                .where(
                    FinancialCandidate.activity_ref == activity.ref_id,
                    FinancialCandidate.expense_ref != ref,
                )
                .limit(1)
            )
            if conflicting or candidate.activity_ref not in (None, activity.ref_id):
                raise rejected("activity_link_conflict")
            if choice.instrument_ref and posting.get("instrument_ref") not in (
                None,
                choice.instrument_ref,
            ):
                raise rejected("activity_instrument_conflict")
            candidate.activity_ref = activity.ref_id
            if activity.instrument_revision_ref:
                pinned_instrument = activity.instrument_revision_ref
                candidate.instrument_revision_ref = pinned_instrument
        if choice.instrument_ref and not pinned_instrument:
            funding = FundingService(session)
            candidate.instrument_revision_ref = funding.revision(
                funding.head(choice.instrument_ref)
            ).ref_id
        if candidate.expense_revision_ref is None:
            candidate.expense_revision_ref = expenses.revision(expenses.head(ref)).ref_id
        candidate.version += 1
        candidate.disposition, candidate.expense_ref = effect, ref
        candidate.review_reason = None
        candidate.changeset_ref = changeset.ref_id
        session.flush()
        return [candidate.ref_id, ref]

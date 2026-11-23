"""Owner-scoped funding revisions, source-bound statement imports and activity reads."""

import csv
import io
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import AttachmentEvidence, AuthenticatedRequest, Entity
from docket.models.funding import AccountActivity, FundingIdentity, FundingRevision, StatementImport
from docket.schemas.funding import (
    ActivityFields,
    FundingAccountSpec,
    FundingContext,
    InstrumentSpec,
    StatementFields,
)
from docket.services.attachment_evidence import AttachmentEvidenceService
from docket.services.native_context import NativeContextService
from docket.services.work_attribution import WorkAttributionService


def funding_error(code: str) -> DocketError:
    return DocketError(code=code, message=code.replace("_", " ") + ".")


def attachment_store(session: Session, settings: Settings) -> AttachmentEvidenceService:
    return AttachmentEvidenceService(
        session,
        encryption_key=settings.attachment_encryption_key(),
        encryption_key_ref=settings.attachment_encryption_key_ref,
        max_attachment_bytes=settings.attachment_max_bytes,
        max_total_bytes=settings.attachment_total_max_bytes,
    )


class FundingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, Any]:
        return {
            "funding_account": self.identity,
            "payment_instrument": self.identity,
            "account_activity": self.activity,
            "statement_import": self.statement,
        }

    def head(self, ref: str, *, lock: bool = False) -> FundingIdentity:
        stmt = select(FundingIdentity).where(
            FundingIdentity.ref_id == ref,
            FundingIdentity.operator_ref == require_principal("read").operator_ref,
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalar(stmt)
        if row is None:
            raise funding_error("funding_identity_unavailable")
        return row

    def require_owned(self, row: Any) -> None:
        if isinstance(row, FundingRevision):
            self.head(row.identity_ref)
        elif isinstance(row, StatementImport):
            self.head(row.account_ref)
        elif row.operator_ref != require_principal("read").operator_ref:
            raise funding_error("funding_record_unavailable")

    def revision(self, head: FundingIdentity) -> FundingRevision:
        row = self.session.scalar(
            select(FundingRevision).where(
                FundingRevision.identity_ref == head.ref_id, FundingRevision.version == head.version
            )
        )
        assert row is not None
        return row

    def identity(self, session: Session, changeset: Any, change: Any) -> list[str]:
        is_account = change.object_type == "funding_account"
        spec = FundingAccountSpec if is_account else InstrumentSpec
        prior: dict[str, Any] = {}
        if change.object_ref:
            head = self.head(change.object_ref, lock=True)
            WorkAttributionService.check_version(head, changeset)
            prior = self.revision(head).fields
            values = change.payload
        else:
            head = FundingIdentity(
                ref_id=new_public_ref("fund" if is_account else "card"),
                kind=change.object_type,
                operator_ref=require_principal("commit").operator_ref,
                version=0,
            )
            values = change.create_spec
        patch = values.model_dump(exclude_unset=True) if hasattr(values, "model_dump") else values
        try:
            fields = spec.model_validate({**prior, **patch}).model_dump(
                mode="json", exclude_none=True
            )
        except ValidationError as exc:
            raise funding_error("invalid_funding_identity_fields") from exc
        for key in ("cardholder_ref", "owner_ref", "liable_party_ref"):
            if (
                fields.get(key)
                and self.session.scalar(select(Entity.id).where(Entity.ref_id == fields[key]))
                is None
            ):
                raise funding_error("funding_party_unavailable")
        if fields.get("account_ref"):
            self.head(fields["account_ref"])
        replaces = fields.get("replaces_ref")
        visited = {head.ref_id}
        while replaces:
            if replaces in visited:
                raise funding_error("funding_replacement_cycle")
            visited.add(replaces)
            predecessor = self.head(replaces, lock=True)
            replaces = self.revision(predecessor).fields.get("replaces_ref")
        self.session.add(head)
        head.version += 1
        self.session.flush()
        rev = FundingRevision(
            identity_ref=head.ref_id,
            version=head.version,
            fields=fields,
            changeset_ref=changeset.ref_id,
        )
        self.session.add(rev)
        self.session.flush()
        WorkAttributionService(self.session).audit(head, rev.ref_id, changeset, change)
        return [head.ref_id, rev.ref_id]

    def observe(self, fields: ActivityFields, changeset: Any, fallback: str) -> AccountActivity:
        account = self.head(fields.account_ref, lock=True)
        card = self.revision(self.head(fields.instrument_ref)) if fields.instrument_ref else None
        if card and card.fields.get("account_ref") not in (None, account.ref_id):
            raise funding_error("instrument_account_conflict")
        data = fields.model_dump(mode="json", exclude_none=True)
        data["amount"] = format(Decimal(fields.amount).normalize(), "f")
        if fields.corrects_ref:
            previous = self.session.get(AccountActivity, fields.corrects_ref)
            if previous is None or previous.account_ref != fields.account_ref:
                raise funding_error("activity_correction_source_unavailable")
        key = sha256_json([fields.external_id or fallback, fields.corrects_ref])
        row = self.session.scalar(
            select(AccountActivity).where(
                AccountActivity.account_ref == account.ref_id, AccountActivity.row_key == key
            )
        )
        if row is not None:
            if row.fields != data:
                raise funding_error("activity_identity_conflict")
            return row
        row = AccountActivity(
            operator_ref=account.operator_ref,
            account_ref=account.ref_id,
            account_revision_ref=self.revision(account).ref_id,
            instrument_revision_ref=card.ref_id if card else None,
            row_key=key,
            fields=data,
            changeset_ref=changeset.ref_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def activity(self, session: Session, changeset: Any, change: Any) -> list[str]:
        fields = ActivityFields.model_validate(change.create_spec)
        row = self.observe(fields, changeset, changeset.ref_id + "/" + change.change_id)
        return [row.ref_id]

    def source(self, ref: str) -> AttachmentEvidence:
        evidence = self.session.scalar(
            select(AttachmentEvidence)
            .join(
                AuthenticatedRequest,
                AttachmentEvidence.authenticated_request_ref == AuthenticatedRequest.ref_id,
            )
            .where(
                AttachmentEvidence.ref_id == ref,
                AuthenticatedRequest.operator_ref == require_principal("read").operator_ref,
                AttachmentEvidence.ingest_state == "available",
            )
        )
        if evidence is None:
            raise funding_error("owned_statement_bytes_required")
        return evidence

    def statement_rows(self, spec: StatementFields) -> list[tuple[int, ActivityFields | str]]:
        self.source(spec.source_ref)
        original = attachment_store(self.session, get_settings()).plaintext(spec.source_ref)
        rows: list[tuple[int, ActivityFields | str]] = []
        try:
            reader = csv.DictReader(
                io.StringIO(original.decode("utf-8-sig"), newline=""), strict=True
            )
            required = {"posted_on", "description", "amount", "currency"}
            allowed = required | {"external_id", "kind", "state", "corrects_ref"}
            header = reader.fieldnames or []
            if len(header) != len(set(header)) or not required <= set(header) <= allowed:
                raise funding_error("unsupported_statement_header")
            for index, raw in enumerate(reader, 1):
                if index > 2000:
                    raise funding_error("statement_row_limit")
                try:
                    if None in raw or any(not raw.get(k) for k in required):
                        raise ValueError
                    values = {k: v for k, v in raw.items() if v}
                    record = ActivityFields.model_validate(
                        {
                            **values,
                            "account_ref": spec.account_ref,
                            "instrument_ref": spec.instrument_ref,
                        }
                    )
                except ValueError:
                    rows.append((index, "invalid_row"))
                    continue
                if not spec.period_start <= record.posted_on <= spec.period_end:
                    rows.append((index, "outside_period"))
                else:
                    rows.append((index, record))
        except (UnicodeError, csv.Error) as exc:
            raise funding_error("invalid_statement_file") from exc
        return rows

    def statement_preview(self, spec: StatementFields) -> dict[str, Any]:
        parsed = self.statement_rows(spec)
        eligible = [r for _, r in parsed if isinstance(r, ActivityFields)]
        totals: dict[str, Decimal] = {}
        for r in eligible:
            totals[r.currency] = totals.get(r.currency, Decimal(0)) + Decimal(r.amount)
        check = "unknown"
        if spec.opening_balance is not None and spec.closing_balance is not None:
            if len(eligible) == len(parsed) and set(totals) <= {spec.balance_currency}:
                difference = spec.closing_balance - spec.opening_balance
                check = (
                    "balanced"
                    if totals.get(spec.balance_currency or "", Decimal(0)) == difference
                    else "mismatch"
                )
            else:
                check = "partial"
        return {
            **spec.model_dump(mode="json", exclude_none=True),
            "date_format": "YYYY-MM-DD",
            "sign": "outflows_negative",
            "rows": len(parsed),
            "valid_rows": len(eligible),
            "invalid_rows": len(parsed) - len(eligible),
            "balance_check": check,
            "currency_totals": {k: str(v) for k, v in totals.items()},
        }

    def statement(self, session: Session, changeset: Any, change: Any) -> list[str]:
        spec = StatementFields.model_validate(change.create_spec)
        account = self.head(spec.account_ref, lock=True)
        evidence = self.source(spec.source_ref)
        basis = {**spec.model_dump(mode="json"), "digest": evidence.content_hash}
        previous = self.session.scalar(
            select(StatementImport).where(
                StatementImport.account_ref == account.ref_id,
                StatementImport.source_ref == spec.source_ref,
            )
        )
        if previous:
            if previous.basis != basis:
                raise funding_error("statement_mapping_conflict")
            return [previous.ref_id]
        overlapping = self.session.scalar(
            select(StatementImport.ref_id)
            .where(
                StatementImport.account_ref == account.ref_id,
                StatementImport.basis["period_start"].as_string() <= spec.period_end.isoformat(),
                StatementImport.basis["period_end"].as_string() >= spec.period_start.isoformat(),
                StatementImport.basis["digest"].as_string() != evidence.content_hash,
            )
            .limit(1)
        )
        results = []
        for index, record in self.statement_rows(spec):
            if isinstance(record, str):
                results.append({"row": index, "status": record})
                continue
            if not record.external_id and overlapping:
                results.append({"row": index, "status": "review_overlapping_period"})
                continue
            try:
                observed = self.observe(record, changeset, f"{evidence.content_hash}/{index}")
            except DocketError as exc:
                results.append({"row": index, "status": exc.code})
            else:
                results.append({"row": index, "ref": observed.ref_id})
        report = StatementImport(
            account_ref=account.ref_id,
            source_ref=spec.source_ref,
            basis=basis,
            rows=results,
            changeset_ref=changeset.ref_id,
        )
        self.session.add(report)
        self.session.flush()
        return [report.ref_id]

    def context(self, query: FundingContext, settings: Settings) -> dict[str, Any]:
        owner = require_principal("read").operator_ref
        stmt = select(FundingIdentity).where(FundingIdentity.operator_ref == owner)
        if query.after_ref:
            stmt = stmt.where(FundingIdentity.ref_id > query.after_ref)
        if query.refs:
            stmt = stmt.where(FundingIdentity.ref_id.in_(query.refs))
        identities = list(self.session.scalars(stmt.order_by(FundingIdentity.ref_id).limit(13)))
        versions = {r.ref_id: r.version for r in identities[:12]}
        objects = [
            {
                "ref": r.ref_id,
                "type": r.kind,
                "version": r.version,
                "state": self.revision(r).fields,
                "revision_ref": self.revision(r).ref_id,
            }
            for r in identities[:12]
        ]
        for ref in query.refs:
            if ref.startswith("ent_"):
                entity = self.session.scalar(select(Entity).where(Entity.ref_id == ref))
                if entity is None:
                    raise funding_error("funding_party_unavailable")
                versions[ref] = entity.version
            if ref.startswith("src_"):
                self.source(ref)
                objects.append({"ref": ref, "type": "source"})
            if ref.startswith(("actv_", "furev_", "fstmt_")):
                row: Any
                if ref.startswith("actv_"):
                    row = self.session.get(AccountActivity, ref)
                elif ref.startswith("furev_"):
                    row = self.session.get(FundingRevision, ref)
                else:
                    row = self.session.get(StatementImport, ref)
                if row is None:
                    raise funding_error("funding_record_unavailable")
                self.require_owned(row)
                state = (
                    row.fields
                    if not isinstance(row, StatementImport)
                    else {"count": len(row.rows), "rows": row.rows[:20], "basis": row.basis}
                )
                objects.append({"ref": ref, "state": state, "changeset_ref": row.changeset_ref})
        if query.account_ref:
            self.head(query.account_ref)
            activity = self.session.scalars(
                select(AccountActivity)
                .where(
                    AccountActivity.account_ref == query.account_ref,
                    AccountActivity.ref_id > (query.after_ref or ""),
                )
                .order_by(AccountActivity.ref_id)
                .limit(13)
            )
            page = list(activity)
            objects += [
                {
                    "ref": a.ref_id,
                    "state": a.fields,
                    "account_revision_ref": a.account_revision_ref,
                    "instrument_revision_ref": a.instrument_revision_ref,
                }
                for a in page[:12]
            ]
            next_ref = page[11].ref_id if len(page) > 12 else None
        else:
            next_ref = identities[11].ref_id if len(identities) > 12 else None
        return NativeContextService(self.session, settings).retain(
            "funding", {"objects": objects, "next_after_ref": next_ref}, versions
        )

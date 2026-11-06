"""Effective policy instructions compile and commit through the canonical engine."""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.models import (
    AuditEvent,
    ChangeSet,
    CompensationPolicy,
    CompensationPolicyRevision,
    CompensationRevisionTerm,
    CompensationTerm,
    Engagement,
    Entity,
    NativeContext,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    EngagementPayTermsSet,
    EngagementReportingRulesSet,
    mutation_input_json,
)
from docket.schemas.compensation import (
    PayTermsFields,
    PolicySetSpec,
    PolicyTermSpec,
    ReportingRulesFields,
    SetPayTerms,
    SetReportingRules,
)
from docket.services.business_commands import _named_refs, mutation_plan
from docket.services.native_commands import NativeCommandService
from docket.services.work_attribution import WorkAttributionService, read_operator, utc_instant

POLICY_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_set_pay_terms": SetPayTerms,
    "docket_set_reporting_rules": SetReportingRules,
}


def policy_error(field: str, message: str) -> DocketError:
    return DocketError(
        code="invalid_compensation_policy", message=message, details={"field": field}
    )


def term_spec(row: CompensationTerm) -> PolicyTermSpec:
    return PolicyTermSpec.model_validate(
        {
            **{key: getattr(row, key) for key in PolicyTermSpec.model_fields},
            "start_at": utc_instant(row.start_at),
            "end_at": utc_instant(row.end_at),
        }
    )


def instant(value: datetime) -> datetime:
    normalized = utc_instant(value)
    assert normalized is not None
    return normalized


def date_boundary(day: date, zone_name: str, field: str) -> datetime:
    zone = work_zone(zone_name)
    try:
        value = datetime.combine(day, time(), zone).astimezone(UTC)
        if value.astimezone(zone).date() == day:
            return value
    except (ValueError, OverflowError):
        pass
    raise policy_error(field, "Use a date that exists in the selected timezone.")


class CompensationPolicyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.work = WorkAttributionService(session)

    def head(self, engagement_ref: str, kind: str, component_key: str) -> CompensationPolicy | None:
        return self.session.scalar(
            select(CompensationPolicy).where(
                CompensationPolicy.operator_ref == read_operator(),
                CompensationPolicy.engagement_ref == engagement_ref,
                CompensationPolicy.kind == kind,
                CompensationPolicy.component_key == component_key,
            )
        )

    def revision(self, head: CompensationPolicy) -> CompensationPolicyRevision:
        row = self.session.scalar(
            select(CompensationPolicyRevision).where(
                CompensationPolicyRevision.policy_ref == head.ref_id,
                CompensationPolicyRevision.revision == head.version,
                CompensationPolicyRevision.sealed.is_(True),
            )
        )
        assert row is not None
        return row

    def terms(self, revision_ref: str) -> list[CompensationTerm]:
        return list(
            self.session.scalars(
                select(CompensationTerm)
                .join(
                    CompensationRevisionTerm,
                    CompensationRevisionTerm.term_ref == CompensationTerm.ref_id,
                )
                .where(CompensationRevisionTerm.revision_ref == revision_ref)
                .order_by(CompensationRevisionTerm.ordinal)
            )
        )

    def validate_payer(self, spec: PolicyTermSpec) -> None:
        if spec.payer_ref:
            row = self.session.scalar(select(Entity).where(Entity.ref_id == spec.payer_ref))
            if (
                row is None
                or row.entity_kind not in {"person", "organization", "institution"}
                or row.canonical_status == "retracted"
            ):
                raise policy_error(
                    "payer_ref", "Select an available person, organization or institution."
                )

    def defaults(self, engagement_ref: str, term: PolicyTermSpec) -> PolicyTermSpec:
        eng = self.work.head(Engagement, engagement_ref)
        values = (
            {
                "basis": "unknown",
                "money_decimals": 2,
                "payer_ref": self.work.engagement_spec(eng).payer_ref,
            }
            if term.kind == "pay"
            else {
                "week_starts_on": 0,
                "rounding_scope": "period",
                "rounding_mode": "nearest",
                "overtime_scope": "week",
                "group_with_payer": False,
            }
        )
        return PolicyTermSpec.model_validate(
            {
                **values,
                "kind": term.kind,
                "timezone": term.timezone,
                "start_at": term.start_at,
                "end_at": term.end_at,
            }
        )

    def overlay(
        self, previous: PolicyTermSpec, spec: PolicySetSpec, start: datetime, end: datetime | None
    ) -> PolicyTermSpec:
        changed = set(spec.changed_fields)
        fields = previous.model_dump()
        values = {key: getattr(spec.term, key) for key in changed}
        fields.update(values)
        if previous.kind == "pay":
            if "rate" in changed and values["rate"] is not None and "basis" not in changed:
                fields["basis"] = "hourly"
            if fields.get("basis") != "hourly" and "rate" not in changed:
                fields["rate"] = None
            if fields.get("basis") not in {"salary", "fixed_fee"} and "amount" not in changed:
                fields["amount"] = None
        else:
            for left, right in (
                ("period_days", "anchor_date"),
                ("overtime_after_hours", "overtime_multiplier"),
            ):
                if left in changed and values[left] is None and right not in changed:
                    fields[right] = None
                if right in changed and values[right] is None and left not in changed:
                    fields[left] = None
            if (fields.get("period_days") is None) != (fields.get("anchor_date") is None):
                raise policy_error(
                    "period_days", "The selected window needs period_days and anchor_date together."
                )
            if (fields.get("overtime_after_hours") is None) != (
                fields.get("overtime_multiplier") is None
            ):
                raise policy_error(
                    "overtime_multiplier",
                    "The selected window needs an explicit threshold and multiplier.",
                )
        result = PolicyTermSpec.model_validate({**fields, "start_at": start, "end_at": end})
        self.validate_payer(result)
        return result

    def normalize(
        self, public: SetPayTerms | SetReportingRules, fallback_zone: str
    ) -> PolicyTermSpec:
        eng = self.work.head(Engagement, public.engagement_ref)
        kind = "pay" if isinstance(public, SetPayTerms) else "reporting"
        component = public.component_key if isinstance(public, SetPayTerms) else "base"
        head = self.head(eng.ref_id, kind, component)
        rows = self.terms(self.revision(head).ref_id) if head else []
        values = public.model_dump(mode="python", exclude_unset=True)
        zone_name = public.timezone
        if zone_name is None:
            # The applicable rule, not the newest future rate, supplies defaults.
            match = next(
                (
                    row
                    for row in rows
                    if (
                        instant(row.start_at).astimezone(work_zone(row.timezone)).date()
                        <= public.effective_from
                        and (
                            row.end_at is None
                            or public.effective_from
                            < instant(row.end_at).astimezone(work_zone(row.timezone)).date()
                        )
                    )
                ),
                None,
            )
            zone_name = match.timezone if match else fallback_zone
        zone = work_zone(zone_name)
        start = date_boundary(public.effective_from, str(zone), "effective_from")
        end = (
            date_boundary(public.effective_to, str(zone), "effective_to")
            if public.effective_to
            else None
        )
        current = next(
            (
                row
                for row in rows
                if instant(row.start_at) <= start
                and (row.end_at is None or start < instant(row.end_at))
            ),
            None,
        )
        fields = (
            term_spec(current).model_dump()
            if current
            else {
                "kind": kind,
                **(
                    {
                        "basis": "unknown",
                        "money_decimals": 2,
                        "payer_ref": self.work.engagement_spec(eng).payer_ref,
                    }
                    if kind == "pay"
                    else {
                        "week_starts_on": 0,
                        "rounding_scope": "period",
                        "rounding_mode": "nearest",
                        "overtime_scope": "week",
                        "group_with_payer": False,
                    }
                ),
            }
        )
        allowed = (
            PayTermsFields.model_fields if kind == "pay" else ReportingRulesFields.model_fields
        )
        fields.update({key: value for key, value in values.items() if key in allowed})
        if kind == "pay":
            if "rate" in values and values["rate"] is not None and "basis" not in values:
                fields["basis"] = "hourly"
            if fields.get("basis") != "hourly" and "rate" not in values:
                fields["rate"] = None
            if fields.get("basis") not in {"salary", "fixed_fee"} and "amount" not in values:
                fields["amount"] = None
        else:
            for left, right in (
                ("period_days", "anchor_date"),
                ("overtime_after_hours", "overtime_multiplier"),
            ):
                if left in values and values[left] is None and right not in values:
                    fields[right] = None
                if right in values and values[right] is None and left not in values:
                    fields[left] = None
            if (fields.get("period_days") is None) != (fields.get("anchor_date") is None):
                raise policy_error(
                    "period_days",
                    "Set period_days and anchor_date together; clearing either removes the period.",
                )
            if (fields.get("overtime_after_hours") is None) != (
                fields.get("overtime_multiplier") is None
            ):
                raise policy_error(
                    "overtime_multiplier",
                    "Set the configured threshold and multiplier together. No rule is assumed.",
                )
        spec = PolicyTermSpec.model_validate(
            {**fields, "kind": kind, "start_at": start, "end_at": end, "timezone": str(zone)}
        )
        self.validate_payer(spec)
        return spec

    def add_term(
        self,
        head: CompensationPolicy,
        spec: PolicyTermSpec,
        changeset_ref: str,
        derived_from: str | None = None,
    ) -> CompensationTerm:
        fields = spec.model_dump()
        fields = {
            key: format(value if value else Decimal(0), "f")
            if isinstance(value, Decimal)
            else value
            for key, value in fields.items()
        }
        row = CompensationTerm(
            policy_ref=head.ref_id,
            derived_from_ref=derived_from,
            created_by_changeset_ref=changeset_ref,
            **fields,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def apply(self, changeset: ChangeSet, change: Any) -> list[str]:
        spec = PolicySetSpec.model_validate(change.payload)
        required = "pay" if change.mutation_type == "engagement_pay_terms_set" else "reporting"
        if spec.term.kind != required or (required == "reporting" and spec.component_key != "base"):
            raise policy_error("engagement_ref", "Policy instruction and terms must match.")
        allowed_fields = set(
            PayTermsFields.model_fields if required == "pay" else ReportingRulesFields.model_fields
        ) | {"timezone"}
        if set(spec.changed_fields) - allowed_fields or spec.defaults.kind != required:
            raise policy_error(
                "engagement_ref", "Policy patch contains fields from another domain."
            )
        work_zone(spec.term.timezone)
        work = self.work
        eng = work.head(Engagement, change.object_ref, lock=True)
        work.check_version(eng, changeset)
        self.validate_payer(spec.term)
        head = self.head(eng.ref_id, required, spec.component_key)
        old = self.terms(self.revision(head).ref_id) if head else []
        if head is None:
            head = CompensationPolicy(
                operator_ref=eng.operator_ref,
                engagement_ref=eng.ref_id,
                kind=required,
                component_key=spec.component_key,
                version=1,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(head)
            self.session.flush()
        else:
            head.version += 1
            head.updated_at = utc_now()
        retained: list[CompensationTerm] = []
        overlapping: list[PolicyTermSpec] = []
        start, end = spec.term.start_at, spec.term.end_at
        for row in old:
            previous = term_spec(row)
            if (previous.end_at is not None and previous.end_at <= start) or (
                end is not None and previous.start_at >= end
            ):
                retained.append(row)
                continue
            if previous.start_at < start:
                retained.append(
                    self.add_term(
                        head,
                        previous.model_copy(update={"end_at": start}),
                        changeset.ref_id,
                        row.ref_id,
                    )
                )
            overlapping.append(previous)
            if end is not None and (previous.end_at is None or previous.end_at > end):
                retained.append(
                    self.add_term(
                        head,
                        previous.model_copy(update={"start_at": end}),
                        changeset.ref_id,
                        row.ref_id,
                    )
                )
        cursor: datetime | None = start
        for previous in overlapping:
            assert cursor is not None
            lo = max(previous.start_at, start)
            hi = (
                previous.end_at
                if end is None
                else end
                if previous.end_at is None
                else min(end, previous.end_at)
            )
            if cursor < lo:
                retained.append(
                    self.add_term(
                        head, self.overlay(spec.defaults, spec, cursor, lo), changeset.ref_id
                    )
                )
            retained.append(
                self.add_term(head, self.overlay(previous, spec, lo, hi), changeset.ref_id)
            )
            cursor = hi
        if cursor is not None and (end is None or cursor < end):
            retained.append(
                self.add_term(
                    head, self.overlay(spec.defaults, spec, cursor, end), changeset.ref_id
                )
            )
        retained.sort(key=lambda row: instant(row.start_at))
        revision = CompensationPolicyRevision(
            policy_ref=head.ref_id,
            revision=head.version,
            created_by_changeset_ref=changeset.ref_id,
            basis_refs=list(change.basis_refs),
            sealed=False,
        )
        self.session.add(revision)
        self.session.flush()
        for ordinal, term in enumerate(retained):
            self.session.add(
                CompensationRevisionTerm(
                    revision_ref=revision.ref_id, term_ref=term.ref_id, ordinal=ordinal
                )
            )
        self.session.flush()
        revision.sealed = True
        self.session.flush()
        refs = work.persist_engagement(changeset, change, work.engagement_spec(eng), head=eng)
        self.session.add(
            AuditEvent(
                event_type="compensation_policy.set",
                entity_type="compensation_policy",
                entity_id=head.id,
                actor_type="operator",
                actor_id=require_principal("commit").operator_ref,
                primary_ref=head.ref_id,
                affected_refs=[head.ref_id, revision.ref_id],
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "change_id": change.change_id,
                    "revision_ref": revision.ref_id,
                    "version": head.version,
                },
            )
        )
        return [*refs, head.ref_id, revision.ref_id, *[term.ref_id for term in retained]]

    def summaries(self, engagement_ref: str) -> list[dict[str, Any]]:
        heads = self.session.scalars(
            select(CompensationPolicy)
            .where(
                CompensationPolicy.engagement_ref == engagement_ref,
                CompensationPolicy.operator_ref == read_operator(),
            )
            .order_by(CompensationPolicy.kind, CompensationPolicy.component_key)
            .limit(6)
        ).all()
        result = [
            {"ref": head.ref_id, "version": head.version, **self.view(head)} for head in heads[:5]
        ]
        if len(heads) > 5:
            result.append({"more_policies": True})
        return result

    def owned(self, ref: str) -> CompensationPolicy:
        head = self.session.scalar(
            select(CompensationPolicy).where(
                CompensationPolicy.ref_id == ref,
                CompensationPolicy.operator_ref == read_operator(),
            )
        )
        if head is None:
            raise policy_error("ref", "Select an owned compensation policy.")
        self.work.head(Engagement, head.engagement_ref)
        return head

    def view(self, head: CompensationPolicy) -> dict[str, Any]:
        revision = self.revision(head)
        rows = self.terms(revision.ref_id)
        now = utc_now()
        current = next(
            (
                index
                for index, row in enumerate(rows)
                if instant(row.start_at) <= now
                and (row.end_at is None or now < instant(row.end_at))
            ),
            None,
        )
        start = (
            current
            if current is not None
            else max(
                0,
                next(
                    (index - 1 for index, row in enumerate(rows) if instant(row.start_at) > now),
                    len(rows) - 3,
                ),
            )
        )
        return {
            "engagement_ref": head.engagement_ref,
            "kind": head.kind,
            "component_key": head.component_key,
            "revision_ref": revision.ref_id,
            "term_count": len(rows),
            "active_term_ref": rows[current].ref_id if current is not None else None,
            "terms": [
                {"ref": row.ref_id, **term_spec(row).model_dump(mode="json", exclude_none=True)}
                for row in rows[start : start + 3]
            ],
            "coverage": "selected effective windows" if start > 0 or len(rows) > 3 else "complete",
            "history": {
                "ref": revision.ref_id,
                "view": "audit",
                "representation": "policy_json_chunk",
            },
        }


class CompensationPolicyCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = (
            SetPayTerms.model_validate(arguments)
            if command == "docket_set_pay_terms"
            else SetReportingRules.model_validate(arguments)
        )
        values = public.model_dump(mode="json", exclude_unset=True)
        service = CompensationPolicyService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            term = service.normalize(public, self.settings.timezone)
            variant = (
                EngagementPayTermsSet
                if isinstance(public, SetPayTerms)
                else EngagementReportingRulesSet
            )
            component = public.component_key if isinstance(public, SetPayTerms) else "base"
            action = variant(
                change_id="compensation",
                action="update",
                object_type="engagement",
                object_ref=public.engagement_ref,
                affected_fields=["compensation_policy"],
                payload=PolicySetSpec(
                    component_key=component,
                    term=term,
                    defaults=service.defaults(public.engagement_ref, term),
                    changed_fields=sorted(
                        set(values).intersection(
                            set(
                                PayTermsFields.model_fields
                                if isinstance(public, SetPayTerms)
                                else ReportingRulesFields.model_fields
                            )
                            | ({"timezone"} if public.timezone is not None else set()),
                        )
                    ),
                ),
            )
            retained = _named_refs(
                service.work.engagement_spec(
                    service.work.head(Engagement, public.engagement_ref)
                ).model_dump(mode="json")
            )
            head = service.head(public.engagement_ref, term.kind, component)
            if head:
                retained.update(
                    ref
                    for row in service.terms(service.revision(head).ref_id)
                    for ref in _named_refs(term_spec(row).model_dump(mode="json"))
                )
            plan = mutation_plan(
                context, [mutation_input_json(action)], command, retained_refs=retained
            )
            plan["assembly_scope"]["planned_create_types"] = ["compensation_policy"]
            return plan

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

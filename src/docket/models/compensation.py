"""Versioned pay/reporting timelines with reusable immutable terms."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class CompensationPolicy(Base):
    __tablename__ = "compensation_policies"
    __table_args__ = (
        UniqueConstraint(
            "engagement_ref", "kind", "component_key", name="uq_compensation_policy_stream"
        ),
        CheckConstraint("version >= 1", name="ck_compensation_policy_version"),
        CheckConstraint("kind IN ('pay', 'reporting')", name="ck_compensation_policy_kind"),
        CheckConstraint(
            "kind <> 'reporting' OR component_key = 'base'", name="ck_compensation_reporting_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("cpol")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    engagement_ref: Mapped[str] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    component_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompensationPolicyRevision(Base):
    __tablename__ = "compensation_policy_revisions"
    __table_args__ = (
        UniqueConstraint("policy_ref", "revision", name="uq_compensation_policy_revision"),
        CheckConstraint("revision >= 1", name="ck_compensation_policy_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("cprv")
    )
    policy_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_policies.ref_id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompensationTerm(Base):
    __tablename__ = "compensation_terms"
    __table_args__ = (
        CheckConstraint("kind IN ('pay', 'reporting')", name="ck_compensation_term_kind"),
        CheckConstraint("end_at IS NULL OR end_at > start_at", name="ck_compensation_term_window"),
        CheckConstraint(
            "basis IS NULL OR basis IN ('unknown', 'hourly', 'salary', 'fixed_fee', 'unpaid')",
            name="ck_compensation_basis",
        ),
        CheckConstraint(
            "money_decimals IS NULL OR (money_decimals >= 0 AND money_decimals <= 9)",
            name="ck_compensation_money_precision",
        ),
        CheckConstraint(
            "week_starts_on IS NULL OR (week_starts_on >= 0 AND week_starts_on <= 6)",
            name="ck_compensation_week_start",
        ),
        CheckConstraint(
            "period_days IS NULL OR (period_days >= 1 AND period_days <= 366)",
            name="ck_compensation_period",
        ),
        CheckConstraint(
            "(period_days IS NULL) = (anchor_date IS NULL)", name="ck_compensation_period_anchor"
        ),
        CheckConstraint(
            "rounding_scope IS NULL OR rounding_scope IN ('segment', 'session', 'day', 'period')",
            name="ck_compensation_rounding_scope",
        ),
        CheckConstraint(
            "rounding_mode IS NULL OR rounding_mode IN ('nearest', 'up', 'down')",
            name="ck_compensation_rounding_mode",
        ),
        CheckConstraint(
            "overtime_scope IS NULL OR overtime_scope IN ('day', 'week')",
            name="ck_compensation_overtime_scope",
        ),
        CheckConstraint(
            "(overtime_after_hours IS NULL) = (overtime_multiplier IS NULL)",
            name="ck_compensation_overtime_pair",
        ),
        CheckConstraint(
            "(kind = 'pay' AND basis IS NOT NULL AND money_decimals IS NOT NULL "
            "AND week_starts_on IS NULL AND period_days IS NULL AND anchor_date IS NULL "
            "AND rounding_minutes IS NULL AND rounding_scope IS NULL AND rounding_mode IS NULL "
            "AND hour_limit_week IS NULL AND overtime_after_hours IS NULL "
            "AND overtime_multiplier IS NULL "
            "AND overtime_scope IS NULL AND group_with_payer IS NULL) OR "
            "(kind = 'reporting' AND basis IS NULL AND rate IS NULL AND amount IS NULL "
            "AND currency IS NULL AND payer_ref IS NULL AND compensable IS NULL "
            "AND billable IS NULL AND money_decimals IS NULL AND week_starts_on IS NOT NULL "
            "AND rounding_scope IS NOT NULL AND rounding_mode IS NOT NULL "
            "AND overtime_scope IS NOT NULL AND group_with_payer IS NOT NULL)",
            name="ck_compensation_term_domain",
        ),
        CheckConstraint("rate IS NULL OR basis = 'hourly'", name="ck_compensation_hourly_rate"),
        CheckConstraint(
            "amount IS NULL OR basis IN ('salary', 'fixed_fee')",
            name="ck_compensation_nonhourly_amount",
        ),
        Index("ix_compensation_term_window", "policy_ref", "start_at", "end_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("cptr")
    )
    policy_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_policies.ref_id", ondelete="RESTRICT")
    )
    derived_from_ref: Mapped[str | None] = mapped_column(
        ForeignKey("compensation_terms.ref_id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    basis: Mapped[str | None] = mapped_column(String(32))
    rate: Mapped[str | None] = mapped_column(String(48))
    amount: Mapped[str | None] = mapped_column(String(48))
    currency: Mapped[str | None] = mapped_column(String(3))
    payer_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    compensable: Mapped[bool | None] = mapped_column(Boolean)
    billable: Mapped[bool | None] = mapped_column(Boolean)
    money_decimals: Mapped[int | None] = mapped_column(Integer)
    week_starts_on: Mapped[int | None] = mapped_column(Integer)
    period_days: Mapped[int | None] = mapped_column(Integer)
    anchor_date: Mapped[date | None] = mapped_column(Date)
    rounding_minutes: Mapped[str | None] = mapped_column(String(48))
    rounding_scope: Mapped[str | None] = mapped_column(String(32))
    rounding_mode: Mapped[str | None] = mapped_column(String(32))
    hour_limit_week: Mapped[str | None] = mapped_column(String(48))
    overtime_after_hours: Mapped[str | None] = mapped_column(String(48))
    overtime_multiplier: Mapped[str | None] = mapped_column(String(48))
    overtime_scope: Mapped[str | None] = mapped_column(String(32))
    group_with_payer: Mapped[bool | None] = mapped_column(Boolean)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompensationRevisionTerm(Base):
    __tablename__ = "compensation_revision_terms"
    __table_args__ = (
        UniqueConstraint("revision_ref", "term_ref", name="uq_compensation_revision_term"),
        CheckConstraint("ordinal >= 0", name="ck_compensation_term_order"),
    )

    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_policy_revisions.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    term_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_terms.ref_id", ondelete="RESTRICT"), nullable=False
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Compensation policy history is immutable")


def _seal(_mapper: object, _connection: object, target: CompensationPolicyRevision) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.sealed.history.deleted
    if changed != {"sealed"} or not target.sealed or (prior and prior[0]):
        _immutable(_mapper, _connection, target)


def _head_update(_mapper: object, _connection: object, target: CompensationPolicy) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.version.history.deleted
    if changed - {"version", "updated_at"} or (prior and target.version != prior[0] + 1):
        _immutable(_mapper, _connection, target)


event.listen(CompensationPolicy, "before_update", _head_update)
event.listen(CompensationPolicy, "before_delete", _immutable)
event.listen(CompensationPolicyRevision, "before_delete", _immutable)
event.listen(CompensationPolicyRevision, "before_update", _seal)
for _model in (CompensationTerm, CompensationRevisionTerm):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

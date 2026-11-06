"""Canonical Google Tasks policies and retained synchronization evidence."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class GoogleTaskList(Base):
    __tablename__ = "google_task_lists"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_list_id", name="uq_google_task_list_identity"),
        CheckConstraint(
            "policy_mode IN ('disabled','observe','two_way')", name="ck_google_task_list_policy"
        ),
        CheckConstraint("version >= 1", name="ck_google_task_list_version"),
        CheckConstraint(
            "NOT is_default OR policy_mode = 'two_way'", name="ck_google_task_list_default_policy"
        ),
        Index(
            "uq_google_task_list_default",
            "operator_ref",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tlist")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    provider_list_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    policy_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="disabled")
    policy_ref: Mapped[str | None] = mapped_column(
        ForeignKey(
            "task_list_policy_revisions.ref_id",
            ondelete="RESTRICT",
            name="fk_google_task_list_current_policy",
            use_alter=True,
        )
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reminder_calendar_ref: Mapped[str | None] = mapped_column(
        ForeignKey("calendar_lanes.ref_id", ondelete="RESTRICT")
    )
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskListPolicyRevision(Base):
    __tablename__ = "task_list_policy_revisions"
    __table_args__ = (
        UniqueConstraint("task_list_ref", "revision", name="uq_task_list_policy_revision"),
        CheckConstraint(
            "mode IN ('disabled','observe','two_way')", name="ck_task_list_policy_mode"
        ),
        CheckConstraint("revision >= 1", name="ck_task_list_policy_revision"),
        CheckConstraint(
            "propagate_google_deletion = (mode = 'two_way')",
            name="ck_task_list_policy_fixed_deletion",
        ),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tpol")
    )
    task_list_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_lists.ref_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    propagate_google_deletion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    authorized_request_ref: Mapped[str] = mapped_column(
        ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"), nullable=False
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    grant_epoch: Mapped[int | None] = mapped_column(Integer)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class GoogleTaskObservation(Base):
    __tablename__ = "google_task_observations"
    __table_args__ = (
        UniqueConstraint(
            "task_list_ref",
            "provider_task_id",
            "source_hash",
            name="uq_google_task_observation_version",
        ),
        Index(
            "ix_google_task_observation_identity",
            "task_list_ref",
            "provider_task_id",
            "observed_at",
        ),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tobs")
    )
    task_list_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_lists.ref_id", ondelete="RESTRICT"), nullable=False
    )
    provider_task_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(512))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inventory_generation: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class TaskProviderBinding(Base):
    __tablename__ = "task_provider_bindings"
    __table_args__ = (
        UniqueConstraint(
            "task_list_ref", "provider_task_id", name="uq_task_provider_binding_identity"
        ),
        Index(
            "uq_task_provider_binding_active_task",
            "task_ref",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
        CheckConstraint(
            "generation >= 1 AND version >= 1", name="ck_task_provider_binding_version"
        ),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tbnd")
    )
    task_ref: Mapped[str] = mapped_column(
        ForeignKey("tasks.ref_id", ondelete="RESTRICT"), nullable=False
    )
    task_list_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_lists.ref_id", ondelete="RESTRICT"), nullable=False
    )
    provider_task_id: Mapped[str | None] = mapped_column(String(1024))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    canonical_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(512))
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    limitations: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    observation_ref: Mapped[str | None] = mapped_column(
        ForeignKey("google_task_observations.ref_id", ondelete="RESTRICT")
    )
    correlation: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    marker_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskBindingRevision(Base):
    __tablename__ = "task_binding_revisions"
    __table_args__ = (UniqueConstraint("binding_ref", "revision", name="uq_task_binding_revision"),)

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tbrv")
    )
    binding_ref: Mapped[str] = mapped_column(
        ForeignKey("task_provider_bindings.ref_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    task_list_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_lists.ref_id", ondelete="RESTRICT"), nullable=False
    )
    provider_task_id: Mapped[str | None] = mapped_column(String(1024))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    canonical_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(512))
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    limitations: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    observation_ref: Mapped[str | None] = mapped_column(
        ForeignKey("google_task_observations.ref_id", ondelete="RESTRICT")
    )
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskSyncConflict(Base):
    __tablename__ = "task_sync_conflicts"
    __table_args__ = (
        UniqueConstraint("binding_ref", "source_hash", name="uq_task_sync_conflict_observation"),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("tscf")
    )
    binding_ref: Mapped[str] = mapped_column(
        ForeignKey("task_provider_bindings.ref_id", ondelete="RESTRICT"), nullable=False
    )
    observation_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_observations.ref_id", ondelete="RESTRICT"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    local_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    google_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskConflictResolution(Base):
    __tablename__ = "task_conflict_resolutions"

    conflict_ref: Mapped[str] = mapped_column(
        ForeignKey("task_sync_conflicts.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    selection: Mapped[str] = mapped_column(String(16), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskSyncState(Base):
    __tablename__ = "task_sync_states"

    task_list_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_lists.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_token: Mapped[str | None] = mapped_column(String(4096))
    generation: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_updated_min: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_policy_ref: Mapped[str | None] = mapped_column(
        ForeignKey("task_list_policy_revisions.ref_id", ondelete="RESTRICT")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TaskPolicyEffect(Base):
    """An exact deterministic import admission, never a fabricated user request."""

    __tablename__ = "task_policy_effects"
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("teff")
    )
    policy_ref: Mapped[str] = mapped_column(
        ForeignKey("task_list_policy_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    observation_ref: Mapped[str] = mapped_column(
        ForeignKey("google_task_observations.ref_id", ondelete="RESTRICT"), nullable=False
    )
    effect_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    planner_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskReminderGeneration(Base):
    __tablename__ = "task_reminder_generations"
    __table_args__ = (
        UniqueConstraint("task_ref", "generation", name="uq_task_reminder_generation"),
        CheckConstraint("generation >= 1", name="ck_task_reminder_generation"),
    )

    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("trgen")
    )
    task_ref: Mapped[str] = mapped_column(
        ForeignKey("tasks.ref_id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    temporal_binding_ref: Mapped[str] = mapped_column(
        ForeignKey("temporal_bindings.ref_id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    projection_ref: Mapped[str] = mapped_column(
        ForeignKey("temporal_calendar_projections.ref_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    plan_ref: Mapped[str] = mapped_column(
        ForeignKey("reminder_plans.ref_id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Task policies and synchronization evidence are immutable")


for _model in (
    TaskListPolicyRevision,
    GoogleTaskObservation,
    TaskBindingRevision,
    TaskSyncConflict,
    TaskConflictResolution,
    TaskPolicyEffect,
    TaskReminderGeneration,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

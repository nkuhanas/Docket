"""One active Google representation with retained baselines and locations."""

from copy import deepcopy
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import TaskBindingRevision, TaskProviderBinding


class TaskBindingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active(self, task_ref: str, *, lock: bool = False) -> TaskProviderBinding | None:
        query = select(TaskProviderBinding).where(
            TaskProviderBinding.task_ref == task_ref,
            TaskProviderBinding.active.is_(True),
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(query)

    def identity(self, listing: str, provider_id: str) -> TaskProviderBinding | None:
        return self.session.scalar(
            select(TaskProviderBinding).where(
                TaskProviderBinding.task_list_ref == listing,
                TaskProviderBinding.provider_task_id == provider_id,
            )
        )

    def previous_identity(self, listing: str, provider_id: str) -> TaskProviderBinding | None:
        ref = self.session.scalar(
            select(TaskBindingRevision.binding_ref)
            .where(
                TaskBindingRevision.task_list_ref == listing,
                TaskBindingRevision.provider_task_id == provider_id,
            )
            .order_by(TaskBindingRevision.created_at.desc())
            .limit(1)
        )
        return self.session.get(TaskProviderBinding, ref) if ref else None

    def retain(self, binding: TaskProviderBinding, basis_refs: list[str]) -> None:
        # Call immediately after assigning the new head fields: no query or
        # intermediate autoflush may expose an unversioned baseline update.
        new = not inspect(binding).persistent
        if new:
            binding.version = 1
        else:
            binding.version += 1
        with self.session.no_autoflush:
            self.session.add(binding)
            self.session.flush()
            self.session.add(
                TaskBindingRevision(
                    binding_ref=binding.ref_id,
                    revision=binding.version,
                    task_list_ref=binding.task_list_ref,
                    provider_task_id=binding.provider_task_id,
                    generation=binding.generation,
                    active=binding.active,
                    detached=binding.detached,
                    canonical_version=binding.canonical_version,
                    provider_etag=binding.provider_etag,
                    provider_updated_at=binding.provider_updated_at,
                    observed_at=binding.observed_at,
                    baseline=deepcopy(binding.baseline),
                    provider_snapshot=deepcopy(binding.provider_snapshot),
                    limitations=deepcopy(binding.limitations),
                    observation_ref=binding.observation_ref,
                    basis_refs=list(dict.fromkeys(basis_refs)),
                )
            )
            self.session.flush()

    @staticmethod
    def require_version(binding: TaskProviderBinding, version: int | None) -> None:
        if version is None or binding.version != version:
            raise DocketError(
                code="task_binding_changed",
                message="Read the task context again before reconciling.",
            )

    @staticmethod
    def status(binding: TaskProviderBinding) -> dict[str, Any]:
        return {
            "binding_ref": binding.ref_id,
            "version": binding.version,
            "task_list_ref": binding.task_list_ref,
            "state": (
                "detached"
                if binding.detached
                else "linked"
                if binding.provider_task_id
                else "pending_creation"
            )
            if binding.active or binding.detached
            else "inactive",
            "limitations": dict(binding.limitations),
            "observation_ref": binding.observation_ref,
        }

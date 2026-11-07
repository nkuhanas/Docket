"""Bounded deterministic imports under an authenticated standing list policy."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuthenticatedRequest,
    GoogleTaskList,
    GoogleTaskObservation,
    OAuthPrincipal,
    ProviderAccount,
    Task,
    TaskListPolicyRevision,
    TaskPolicyEffect,
    TaskProviderBinding,
)
from docket.providers.google.oauth import TASKS_SCOPE, authorized_user_file_status

PLANNER_VERSION = 1


class TaskPolicyAuthority:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session, self.settings = session, settings or get_settings()

    @staticmethod
    def denied() -> DocketError:
        return DocketError(
            code="task_sync_policy_denied",
            message="The task synchronization policy is unavailable.",
        )

    def policy(
        self, ref: str, *, lock: bool = False
    ) -> tuple[TaskListPolicyRevision, GoogleTaskList]:
        if not self.settings.tasks_enabled:
            raise self.denied()
        policy = self.session.get(TaskListPolicyRevision, ref)
        if policy is None or policy.operator_ref != self.settings.canonical_operator_ref:
            raise self.denied()
        from sqlalchemy import select

        listing_query = select(GoogleTaskList).where(GoogleTaskList.ref_id == policy.task_list_ref)
        if lock:
            listing_query = listing_query.with_for_update().execution_options(
                populate_existing=True
            )
        listing = self.session.scalar(listing_query)
        if (
            listing is None
            or listing.operator_ref != policy.operator_ref
            or listing.policy_ref != policy.ref_id
            or listing.policy_mode != "two_way"
            or policy.mode != "two_way"
            or not listing.available
        ):
            raise self.denied()
        account_query = select(ProviderAccount).where(ProviderAccount.id == listing.account_id)
        if lock:
            account_query = account_query.with_for_update().execution_options(
                populate_existing=True
            )
        account = self.session.scalar(account_query)
        if (
            account is None
            or not account.enabled
            or account.provider != "google"
            or "google_tasks" not in account.capabilities
        ):
            raise self.denied()
        request = (
            self.session.query(AuthenticatedRequest)
            .filter(
                AuthenticatedRequest.ref_id == policy.authorized_request_ref,
            )
            .one_or_none()
        )
        if (
            request is None
            or request.principal_ref != policy.principal_ref
            or request.operator_ref != policy.operator_ref
            or request.state == "cancelled"
            or "commit" not in request.permissions
        ):
            raise self.denied()
        if policy.principal_ref.startswith("prn_"):
            principal_query = select(OAuthPrincipal).where(
                OAuthPrincipal.ref_id == policy.principal_ref
            )
            if lock:
                principal_query = principal_query.with_for_update().execution_options(
                    populate_existing=True
                )
            principal = self.session.scalar(principal_query)
            if (
                principal is None
                or not principal.enabled
                or principal.operator_ref != policy.operator_ref
                or principal.issuer != self.settings.plugin_oauth_issuer
                or principal.valid_after != policy.grant_epoch
                or self.settings.plugin_write_scope not in principal.scopes
            ):
                raise self.denied()
        elif policy.principal_ref == "agent:interactive":
            expires = self.settings.interactive_agent_expires_at
            if expires is not None:
                expires = expires.replace(tzinfo=expires.tzinfo or UTC)
            if not self.settings.interactive_agent_enabled or (
                expires is not None and expires <= datetime.now(UTC)
            ):
                raise self.denied()
        else:
            raise self.denied()
        if (
            self.settings.environment.value == "production"
            and authorized_user_file_status(
                self.settings.google_oauth_token_file, required_scopes=[TASKS_SCOPE]
            )
            != "configured"
        ):
            raise self.denied()
        return policy, listing

    def require(
        self, ref: str, content: Any | None = None, *, lock: bool = False
    ) -> TaskPolicyEffect:
        effect = self.session.get(TaskPolicyEffect, ref)
        if effect is None or effect.planner_version != PLANNER_VERSION:
            raise self.denied()
        if effect.reminder_generation_ref is not None:
            from docket.services.task_reminder_inbound import TaskReminderInbound

            TaskReminderInbound(self.session, self.settings).require(effect, content, lock=lock)
            return effect
        if effect.policy_ref is None or effect.observation_ref is None:
            raise self.denied()
        policy, listing = self.policy(effect.policy_ref, lock=lock)
        observation = self.session.get(GoogleTaskObservation, effect.observation_ref)
        if observation is None or observation.task_list_ref != listing.ref_id:
            raise self.denied()
        if content is not None:
            from docket.services.change_sets import _content_payload

            payload = _content_payload(content)
            if effect.effect_hash != sha256_json(payload) or payload != effect.content:
                raise self.denied()
            # Admission covers one observed Task, plus support derived by its
            # ordinary service. It cannot carry arbitrary entity/finance writes.
            if (
                any(
                    getattr(content, field)
                    for field in (
                        "registry_changes",
                        "preference_changes",
                        "lane_changes",
                        "event_changes",
                        "resolution_changes",
                        "provider_intents",
                    )
                )
                or len(content.tracked_context_changes) != 1
            ):
                raise self.denied()
            change = content.tracked_context_changes[0]
            if change.object_type != "task":
                raise self.denied()
            values = change.create_spec if change.action == "create" else change.payload
            if change.action == "create":
                if values.task_list_ref != listing.ref_id or values.item_ref is not None:
                    raise self.denied()
                from sqlalchemy import select

                existing_binding = self.session.scalar(
                    select(TaskProviderBinding).where(
                        TaskProviderBinding.task_list_ref == listing.ref_id,
                        TaskProviderBinding.provider_task_id == observation.provider_task_id,
                    )
                )
                if existing_binding is not None:
                    raise self.denied()
            else:
                from sqlalchemy import select

                task = self.session.scalar(select(Task).where(Task.ref_id == change.object_ref))
                binding = self.session.scalar(
                    select(TaskProviderBinding)
                    .where(
                        TaskProviderBinding.task_ref == change.object_ref,
                        TaskProviderBinding.provider_task_id == observation.provider_task_id,
                        TaskProviderBinding.active.is_(True),
                        TaskProviderBinding.detached.is_(False),
                    )
                    .execution_options(populate_existing=True)
                )
                if task is None or binding is None:
                    raise self.denied()
                if effect.expected_binding_version != binding.version:
                    raise self.denied()
                if binding.task_list_ref != listing.ref_id:
                    source = self.session.get(GoogleTaskList, binding.task_list_ref)
                    if (
                        change.action != "update"
                        or "task_list_ref" not in values.model_fields_set
                        or values.task_list_ref != listing.ref_id
                        or source is None
                        or source.policy_ref is None
                        or source.account_id != listing.account_id
                    ):
                        raise self.denied()
                    self.policy(source.policy_ref, lock=lock)
                if change.action == "retract":
                    if not policy.propagate_google_deletion or not observation.fields.get(
                        "deleted"
                    ):
                        raise self.denied()
                elif change.action != "update" or not values.model_fields_set.issubset(
                    {
                        "title",
                        "description",
                        "scheduled_date",
                        "task_state",
                        "completed_at",
                        "canonical_status",
                        "parent_task_ref",
                        "task_list_ref",
                    }
                ):
                    raise self.denied()
        return effect

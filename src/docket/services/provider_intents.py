from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import OperationStatus
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Item,
    Operation,
    OperationTarget,
    ProviderAccount,
    ProviderEventBinding,
    ReminderPlan,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.models.base import utc_now
from docket.schemas.authority import ProviderIntentInput

_LANE_OPERATION_TYPES = frozenset(
    {"calendar_configure_lane", "calendar_delete_lane"}
)
_EVENT_OPERATION_TYPES = frozenset(
    {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_update_reminders",
        "calendar_cancel_event",
    }
)
_SUPPORTED_OPERATION_TYPES = _LANE_OPERATION_TYPES | _EVENT_OPERATION_TYPES


class ProviderIntentService:
    """Compile provider effects directly from committed canonical targets."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _account(self, account_ref: str) -> ProviderAccount:
        account = self.session.scalar(
            select(ProviderAccount).where(ProviderAccount.ref_id == account_ref)
        )
        if (
            account is None
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="provider_target_unresolved",
                message="Provider intent requires one enabled Calendar account.",
                details={"account_ref": account_ref},
            )
        return account

    @staticmethod
    def _target_refs(
        intent: ProviderIntentInput,
        refs_by_change_id: dict[str, list[str]],
    ) -> list[str]:
        refs = list(intent.canonical_target_refs)
        for change_id in intent.canonical_target_change_ids:
            resolved = refs_by_change_id.get(change_id)
            if resolved is None or len(resolved) != 1:
                raise DocketError(
                    code="provider_target_unresolved",
                    message="Provider target create reference did not resolve exactly once.",
                    details={"change_id": change_id},
                )
            if resolved[0] not in refs:
                refs.append(resolved[0])
        return refs

    def _targets(
        self,
        refs: list[str],
    ) -> tuple[
        CanonicalEvent | None,
        TemporalCalendarProjection | None,
        CalendarLane | None,
    ]:
        event = self.session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id.in_(refs))
        )
        projection = self.session.scalar(
            select(TemporalCalendarProjection).where(
                TemporalCalendarProjection.ref_id.in_(refs)
            )
        )
        lane = self.session.scalar(
            select(CalendarLane).where(CalendarLane.ref_id.in_(refs))
        )
        if event is not None:
            event_lane = self.session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == event.lane_ref)
            )
            if lane is not None and event_lane is not None and lane.id != event_lane.id:
                raise DocketError(
                    code="provider_target_mismatch",
                    message="Provider intent event and lane targets do not match.",
                )
            lane = event_lane or lane
        if projection is not None:
            projection_lane = self.session.scalar(
                select(CalendarLane).where(
                    CalendarLane.ref_id == projection.lane_ref
                )
            )
            if (
                lane is not None
                and projection_lane is not None
                and lane.id != projection_lane.id
            ):
                raise DocketError(
                    code="provider_target_mismatch",
                    message="Provider intent projection and lane targets do not match.",
                )
            lane = projection_lane or lane
        if event is not None and projection is not None:
            raise DocketError(
                code="provider_target_mismatch",
                message="One provider intent cannot target an Event and Time projection.",
            )
        return event, projection, lane

    def _event_binding(
        self,
        *,
        canonical_target_ref: str,
        target_kind: str,
        account: ProviderAccount,
        lane: CalendarLane,
    ) -> ProviderEventBinding:
        binding = self.session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.canonical_target_ref == canonical_target_ref,
                ProviderEventBinding.target_kind == target_kind,
                ProviderEventBinding.account_id == account.id,
                ProviderEventBinding.calendar_id == lane.calendar_id,
                ProviderEventBinding.status == "active",
            )
        )
        if binding is None:
            raise DocketError(
                code="provider_event_binding_required",
                message="Provider event update requires an exact active binding.",
            )
        return binding

    def _temporal_title(self, binding: TemporalBinding) -> str:
        if binding.subject_ref.startswith("item_"):
            item = self.session.scalar(
                select(Item).where(Item.ref_id == binding.subject_ref)
            )
            title = item.title if item is not None else None
        else:
            task = self.session.scalar(
                select(Task).where(Task.ref_id == binding.subject_ref)
            )
            title = task.title if task is not None else None
        if title is None:
            raise DocketError(
                code="temporal_projection_subject_unresolved",
                message="Time projection subject is not available.",
                details={"subject_ref": binding.subject_ref},
            )
        return title

    @staticmethod
    def _temporal_timing(
        binding: TemporalBinding,
        display_policy: dict[str, Any],
    ) -> dict[str, Any]:
        value = binding.temporal_value
        value_kind = value.get("kind")
        policy_kind = display_policy.get("kind")
        if value_kind == "date" and policy_kind == "all_day_marker":
            start = date.fromisoformat(str(value["date"]))
            return {
                "kind": "all_day",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=1)).isoformat(),
                "timezone": str(value["timezone"]),
            }
        if value_kind == "datetime" and policy_kind == "timed_marker":
            start = datetime.fromisoformat(str(value["local_datetime"]))
            duration = int(display_policy["duration_seconds"])
            return {
                "kind": "timed",
                "start_local": start.isoformat(),
                "end_local": (start + timedelta(seconds=duration)).isoformat(),
                "timezone": str(value["timezone"]),
                **({"fold": value["fold"]} if value.get("fold") is not None else {}),
            }
        if value_kind == "date_interval" and policy_kind == "interval_span":
            start = date.fromisoformat(str(value["start_date"]))
            end = date.fromisoformat(str(value["end_date"]))
            if bool(value["end_inclusive"]):
                end += timedelta(days=1)
            return {
                "kind": "all_day",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "timezone": str(value["timezone"]),
            }
        if value_kind == "datetime_interval" and policy_kind == "interval_span":
            return {
                "kind": "timed",
                "start_local": str(value["start_local"]),
                "end_local": str(value["end_local"]),
                "timezone": str(value["timezone"]),
                **({"fold": value["fold"]} if value.get("fold") is not None else {}),
            }
        raise DocketError(
            code="calendar_display_policy_incompatible",
            message="Time value and Calendar display policy are incompatible.",
            details={"temporal_binding_ref": binding.ref_id},
        )

    def _projection_reminder_plan(
        self,
        projection: TemporalCalendarProjection,
    ) -> dict[str, Any] | None:
        plans: list[ReminderPlan]
        if projection.reminder_plan_ref is not None:
            plans = list(
                self.session.scalars(
                    select(ReminderPlan).where(
                        ReminderPlan.ref_id == projection.reminder_plan_ref,
                        ReminderPlan.canonical_status == "active",
                    )
                )
            )
        else:
            plans = list(
                self.session.scalars(
                    select(ReminderPlan).where(
                        ReminderPlan.subject_ref == projection.temporal_binding_ref,
                        ReminderPlan.canonical_status == "active",
                    )
                )
            )
        return self._merged_google_reminders(plans)

    @staticmethod
    def _merged_google_reminders(
        plans: list[ReminderPlan],
    ) -> dict[str, Any] | None:
        google_plans = [
            plan for plan in plans if "google_popup" in plan.delivery_channels
        ]
        if not google_plans:
            return None
        return {
            "delivery_channels": ["google_popup"],
            "lead_seconds": sorted(
                {
                    int(lead)
                    for plan in google_plans
                    for lead in plan.lead_seconds
                }
            ),
        }

    def _event_reminder_plan(self, event: CanonicalEvent) -> dict[str, Any] | None:
        plans = list(
            self.session.scalars(
                select(ReminderPlan).where(
                    ReminderPlan.subject_ref == event.ref_id,
                    ReminderPlan.canonical_status == "active",
                )
            )
        )
        return self._merged_google_reminders(plans)

    def _parameters(
        self,
        operation_type: str,
        *,
        event: CanonicalEvent | None,
        projection: TemporalCalendarProjection | None,
        lane: CalendarLane | None,
        account: ProviderAccount,
        hints: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        if operation_type in _LANE_OPERATION_TYPES:
            if lane is None or lane.account_id != account.id:
                raise DocketError(
                    code="calendar_lane_unresolved",
                    message="Lane provider intent requires one matching CalendarLane.",
                )
            return (
                {
                    "lane_ref": lane.ref_id,
                    "lane": lane.lane,
                    "display_name": lane.display_name,
                    "color_hex": lane.color_hex,
                    "timezone": get_settings().timezone,
                    "calendar_id": lane.calendar_id,
                    "lane_version": lane.version,
                },
                lane.ref_id,
                "calendar_lane",
            )
        if lane is None or lane.account_id != account.id:
            raise DocketError(
                code="canonical_event_target_required",
                message="Calendar provider intent requires one canonical target and lane.",
            )
        if event is not None:
            canonical_target_ref = event.ref_id
            target_kind = "event"
            event_payload = dict(event.event_spec)
            event_payload["title"] = event.title
            reminder_plan = self._event_reminder_plan(event)
        elif projection is not None:
            binding = self.session.scalar(
                select(TemporalBinding).where(
                    TemporalBinding.ref_id == projection.temporal_binding_ref,
                    TemporalBinding.canonical_status == "active",
                )
            )
            if binding is None:
                raise DocketError(
                    code="temporal_binding_not_found",
                    message="Time projection requires one active TemporalBinding.",
                )
            canonical_target_ref = projection.ref_id
            target_kind = "temporal_projection"
            event_payload = {
                "title": self._temporal_title(binding),
                "timing": self._temporal_timing(binding, projection.display_policy),
                "transparency": projection.display_policy.get(
                    "transparency", "transparent"
                ),
                "temporal_role": binding.role,
            }
            reminder_plan = self._projection_reminder_plan(projection)
        else:
            raise DocketError(
                code="canonical_event_target_required",
                message="Calendar provider intent requires an Event or Time projection.",
            )
        parameters: dict[str, Any] = {
            "calendar_id": lane.calendar_id,
            "lane_ref": lane.ref_id,
            "logical_key": f"canonical:{canonical_target_ref}",
            "event": event_payload,
            "reminder_plan": reminder_plan,
            "reminder_plan_sha256": (
                sha256_json(reminder_plan) if isinstance(reminder_plan, dict) else None
            ),
            "priority": event_payload.get("priority", "normal"),
            "priority_basis": "explicit_operator",
            "canonical_target_ref": canonical_target_ref,
            "target_kind": target_kind,
        }
        if operation_type != "calendar_create_event":
            provider_binding = self._event_binding(
                canonical_target_ref=canonical_target_ref,
                target_kind=target_kind,
                account=account,
                lane=lane,
            )
            parameters.update(
                {
                    "external_event_id": provider_binding.provider_event_id,
                    "provider_etag": provider_binding.provider_etag,
                    "provider_before": dict(provider_binding.provider_snapshot),
                }
            )
        if operation_type == "calendar_cancel_event" and hints.get("reason") is not None:
            parameters["reason"] = str(hints["reason"])
        return parameters, canonical_target_ref, target_kind

    def _predecessor(
        self,
        changeset: ChangeSet,
        lane: CalendarLane | None,
    ) -> Operation | None:
        if lane is None or lane.calendar_id is not None:
            return None
        candidates = list(
            self.session.scalars(
                select(Operation).where(
                    Operation.originating_changeset_ref == changeset.ref_id,
                    Operation.operation_type == "calendar_configure_lane",
                )
            )
        )
        return next(
            (item for item in candidates if lane.ref_id in item.canonical_target_refs),
            None,
        )

    def materialize(
        self,
        _session: Session,
        changeset: ChangeSet,
        intent: ProviderIntentInput,
        refs_by_change_id: dict[str, list[str]],
    ) -> list[str]:
        if intent.operation_type not in _SUPPORTED_OPERATION_TYPES:
            raise DocketError(
                code="provider_operation_not_supported",
                message="Provider operation is outside the ChangeSet profile.",
                details={"operation_type": intent.operation_type},
            )
        existing = self.session.scalar(
            select(Operation).where(Operation.idempotency_key == intent.idempotency_key)
        )
        if existing is not None:
            if existing.originating_changeset_ref != changeset.ref_id:
                raise IdempotencyConflict(intent.idempotency_key)
            return [existing.ref_id]

        target_refs = self._target_refs(intent, refs_by_change_id)
        event, projection, lane = self._targets(target_refs)
        account = self._account(intent.account_ref)
        if lane is not None and lane.account_id != account.id:
            raise DocketError(
                code="provider_target_mismatch",
                message="Provider account does not match the canonical lane account.",
            )
        parameters, primary_ref, target_kind = self._parameters(
            intent.operation_type,
            event=event,
            projection=projection,
            lane=lane,
            account=account,
            hints=intent.parameters,
        )
        predecessor = self._predecessor(changeset, lane)
        if (
            intent.operation_type in _EVENT_OPERATION_TYPES
            and lane is not None
            and lane.calendar_id is None
            and predecessor is None
        ):
            raise DocketError(
                code="calendar_lane_provider_binding_unresolved",
                message="An unprovisioned event lane requires same-ChangeSet provisioning.",
                details={"lane_ref": lane.ref_id},
            )

        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            originating_changeset_ref=changeset.ref_id,
            basis_refs=list(intent.basis_refs),
            canonical_target_refs=target_refs,
            predecessor_operation_id=(predecessor.id if predecessor is not None else None),
            idempotency_key=intent.idempotency_key,
            operation_type=intent.operation_type,
            account_id=account.id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=utc_now(),
        )
        self.session.add(operation)
        self.session.flush()
        target = OperationTarget(
            operation_id=operation.id,
            target_key=primary_ref,
            canonical_target_ref=primary_ref,
            target_kind=target_kind,
            idempotency_key=f"{intent.idempotency_key}:target:{primary_ref}",
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            status="pending",
            next_attempt_at=utc_now(),
        )
        self.session.add(target)
        if intent.operation_type == "calendar_configure_lane" and lane is not None:
            lane.status = "provisioning"
            lane.version += 1
        self.session.add(
            AuditEvent(
                event_type="operation.created",
                entity_type="operation",
                entity_id=operation.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=operation.ref_id,
                affected_refs=[operation.ref_id, *target_refs],
                basis_refs=list(intent.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "intent_id": intent.intent_id,
                    "operation_type": intent.operation_type,
                    "target_ref": primary_ref,
                },
            )
        )
        return [operation.ref_id]

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import parse_public_ref
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Entity,
    GmailSource,
    IdentityHandle,
    LaneRoutingDecision,
    Preference,
    ProviderAccount,
    SenderIdentityEmail,
    Source,
)
from docket.models.base import utc_now
from docket.schemas.policy import (
    CalendarLaneCreateSpec,
    LaneRoutingDecisionCreateSpec,
    PreferenceCreateSpec,
)

PolicyHandler = Callable[[Session, ChangeSet, Any], list[str]]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ContextPolicyService:
    """Apply Preferences, CalendarLanes, and routing decisions within a ChangeSet."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def handlers(self) -> dict[str, PolicyHandler]:
        return {
            "preference": self.apply_preference,
            "calendar_lane": self.apply_calendar_lane,
            "lane_routing_decision": self.apply_routing_decision,
        }

    @staticmethod
    def _provenance(changeset: ChangeSet, change: Any) -> dict[str, Any]:
        return {
            "basis_refs": list(change.basis_refs),
            "decision_refs": [
                ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "dec"
            ],
            "source_refs": [ref for ref in change.basis_refs if parse_public_ref(ref)[0] == "src"],
            "created_by_changeset_ref": changeset.ref_id,
        }

    def _audit(
        self,
        *,
        event_type: str,
        item: Any,
        changeset: ChangeSet,
        change: Any,
        affected_refs: list[str],
    ) -> None:
        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type=change.object_type,
                entity_id=item.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                primary_ref=item.ref_id,
                affected_refs=affected_refs,
                basis_refs=list(change.basis_refs),
                data={
                    "changeset_ref": changeset.ref_id,
                    "change_id": change.change_id,
                    "action": change.action,
                },
            )
        )

    def _validate_preference_target(self, spec: PreferenceCreateSpec) -> None:
        target: object | None = None
        if spec.target_type == "entity":
            target = self.session.scalar(select(Entity).where(Entity.ref_id == spec.target_ref))
        elif spec.target_type == "identity":
            target = self.session.scalar(
                select(IdentityHandle).where(IdentityHandle.ref_id == spec.target_ref)
            )
        elif spec.target_type == "source":
            target = self.session.scalar(
                select(GmailSource).where(GmailSource.ref_id == spec.target_ref)
            ) or self.session.scalar(
                select(Source).where(Source.ref_id == spec.target_ref)
            )
        if spec.target_type in {"entity", "identity", "source"} and target is None:
            raise DocketError(
                code="preference_target_not_found",
                message="Preference target must resolve to the declared public-ref type.",
                details={"target_type": spec.target_type, "target_ref": spec.target_ref},
            )
        if isinstance(target, IdentityHandle) and target.status not in {"unbound", "bound"}:
            raise DocketError(
                code="preference_identity_inactive",
                message="Preference identity target is historical or retracted.",
                details={"identity_ref": target.ref_id, "status": target.status},
            )
        if spec.policy_kind == "suppression" and isinstance(target, IdentityHandle):
            if target.handle_type == "sender_label":
                association = self.session.scalar(
                    select(SenderIdentityEmail.id).where(
                        SenderIdentityEmail.sender_identity_handle_id == target.id,
                        SenderIdentityEmail.status == "active",
                    )
                )
                if association is None:
                    raise DocketError(
                        code="sender_identity_email_required",
                        message=(
                            "A sender-label suppression target requires at least one "
                            "active exact email association."
                        ),
                        details={"identity_ref": target.ref_id},
                    )
            elif target.handle_type != "email":
                raise DocketError(
                    code="unsupported_suppression_identity_type",
                    message=(
                        "Email-triage suppression requires an exact email or an "
                        "email-associated sender-label IdentityHandle."
                    ),
                    details={
                        "identity_ref": target.ref_id,
                        "handle_type": target.handle_type,
                    },
                )
        if spec.policy_kind == "calendar_route" and spec.target_type not in {
            "global",
            "entity",
            "semantic_class",
        }:
            raise DocketError(
                code="unsupported_calendar_route_target",
                message="Calendar routing supports entity, semantic-class, or global targets.",
                details={"target_type": spec.target_type},
            )

    def apply_preference(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        if change.action == "create":
            spec = PreferenceCreateSpec.model_validate(change.create_spec)
            self._validate_preference_target(spec)
            existing = self.session.scalar(
                select(Preference).where(Preference.preference_key == spec.preference_key)
            )
            if existing is not None:
                raise DocketError(
                    code="preference_key_exists",
                    message="Preference key already exists; update its exact public ref.",
                    details={"preference_ref": existing.ref_id},
                )
            preference = Preference(
                preference_key=spec.preference_key,
                policy_kind=spec.policy_kind,
                target_type=spec.target_type,
                target_ref=spec.target_ref,
                target_key=None,
                semantic_class=spec.semantic_class,
                policy_text=spec.policy_text,
                policy_json=spec.policy_json,
                scope_json=spec.scope_json,
                priority=spec.priority,
                valid_from=spec.valid_from,
                valid_to=spec.valid_to,
                status=spec.status,
                **self._provenance(changeset, change),
            )
            self.session.add(preference)
            self.session.flush()
        else:
            if change.object_ref is None:
                raise DocketError(
                    code="preference_ref_required",
                    message="Preference mutation requires an exact public ref.",
                )
            stored_preference = self.session.scalar(
                select(Preference).where(Preference.ref_id == change.object_ref)
            )
            if stored_preference is None:
                raise DocketError(code="preference_not_found", message="Preference was not found.")
            preference = stored_preference
            if change.action == "retract":
                preference.status = "retracted"
            elif change.action in {"update", "supersede"}:
                allowed = {
                    "policy_text",
                    "policy_json",
                    "scope_json",
                    "priority",
                    "valid_from",
                    "valid_to",
                    "status",
                }
                unknown = set(change.payload) - allowed
                if unknown:
                    raise DocketError(
                        code="invalid_preference_patch",
                        message="Preference patch contains unsupported fields.",
                        details={"fields": sorted(unknown)},
                    )
                if preference.policy_kind == "suppression":
                    policy_json = change.payload.get("policy_json", preference.policy_json)
                    if (
                        not isinstance(policy_json, dict)
                        or policy_json.get("disposition") != "suppress"
                    ):
                        raise DocketError(
                            code="invalid_suppression_policy",
                            message=(
                                "Suppression policy updates require "
                                "policy_json.disposition='suppress'."
                            ),
                        )
                    if preference.target_type == "identity":
                        target = self.session.scalar(
                            select(IdentityHandle).where(
                                IdentityHandle.ref_id == preference.target_ref
                            )
                        )
                        if target is None:
                            raise DocketError(
                                code="preference_target_not_found",
                                message="Preference identity target was not found.",
                            )
                        if target.handle_type == "sender_label":
                            association = self.session.scalar(
                                select(SenderIdentityEmail.id).where(
                                    SenderIdentityEmail.sender_identity_handle_id == target.id,
                                    SenderIdentityEmail.status == "active",
                                )
                            )
                            if association is None:
                                raise DocketError(
                                    code="sender_identity_email_required",
                                    message=(
                                        "A sender-label suppression target requires an "
                                        "active exact email association."
                                    ),
                                )
                for key, value in change.payload.items():
                    setattr(preference, key, value)
            else:
                raise DocketError(
                    code="unsupported_preference_action", message="Unsupported action."
                )
            preference.version += 1
        self._audit(
            event_type="preference.changed",
            item=preference,
            changeset=changeset,
            change=change,
            affected_refs=[preference.ref_id],
        )
        return [preference.ref_id]

    def apply_calendar_lane(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        if change.action == "create":
            spec = CalendarLaneCreateSpec.model_validate(change.create_spec)
            account = self.session.get(ProviderAccount, spec.account_id)
            if (
                account is None
                or not account.enabled
                or "google_calendar" not in account.capabilities
            ):
                raise DocketError(
                    code="calendar_account_unavailable",
                    message="CalendarLane requires an enabled Calendar-capable account.",
                )
            lane = self.session.scalar(
                select(CalendarLane).where(
                    CalendarLane.account_id == account.id,
                    CalendarLane.lane == spec.name,
                )
            )
            if lane is None:
                values: dict[str, Any] = {}
                if spec.ref_id is not None:
                    values["ref_id"] = spec.ref_id
                lane = CalendarLane(
                    account_id=account.id,
                    lane=spec.name,
                    display_name=spec.display_name,
                    color_hex=spec.color_hex,
                    calendar_id=spec.provider_calendar_binding,
                    operator_policy_text=spec.operator_policy_text,
                    metadata_json=spec.metadata_json,
                    enabled=spec.enabled,
                    priority=spec.priority,
                    status=(
                        "active" if spec.provider_calendar_binding is not None else "unprovisioned"
                    ),
                    **self._provenance(changeset, change),
                    **values,
                )
                self.session.add(lane)
                self.session.flush()
            else:
                raise DocketError(
                    code="calendar_lane_exists",
                    message="CalendarLane already exists; update its exact public ref.",
                    details={"lane_ref": lane.ref_id},
                )
        else:
            if change.object_ref is None:
                raise DocketError(
                    code="calendar_lane_ref_required",
                    message="CalendarLane mutation requires an exact ref.",
                )
            lane = self.session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == change.object_ref)
            )
            if lane is None:
                raise DocketError(
                    code="calendar_lane_not_found", message="CalendarLane was not found."
                )
            if change.action == "retract":
                lane.enabled = False
            elif change.action in {"update", "supersede"}:
                allowed = {
                    "display_name",
                    "operator_policy_text",
                    "metadata_json",
                    "enabled",
                    "priority",
                }
                unknown = set(change.payload) - allowed
                if unknown:
                    raise DocketError(
                        code="invalid_calendar_lane_patch",
                        message="CalendarLane patch contains unsupported fields.",
                        details={"fields": sorted(unknown)},
                    )
                for key, value in change.payload.items():
                    setattr(lane, key, value)
            else:
                raise DocketError(
                    code="unsupported_calendar_lane_action",
                    message="Unsupported action.",
                )
            lane.version += 1
        self._audit(
            event_type="calendar_lane.changed",
            item=lane,
            changeset=changeset,
            change=change,
            affected_refs=[lane.ref_id],
        )
        return [lane.ref_id]

    def apply_routing_decision(
        self,
        _session: Session,
        changeset: ChangeSet,
        change: Any,
    ) -> list[str]:
        if change.action != "create":
            raise DocketError(
                code="routing_decision_immutable",
                message="Routing Decisions are immutable; create a later Decision.",
            )
        spec = LaneRoutingDecisionCreateSpec.model_validate(change.create_spec)
        lane = self.session.scalar(select(CalendarLane).where(CalendarLane.ref_id == spec.lane_ref))
        if lane is None or not lane.enabled:
            raise DocketError(
                code="calendar_lane_unavailable",
                message="Routing Decision requires an enabled CalendarLane.",
            )
        route = LaneRoutingDecision(
            lane_id=lane.id,
            lane_ref=lane.ref_id,
            event_ref=spec.event_ref,
            organization_ref=spec.organization_ref,
            recurring_identity=spec.recurring_identity,
            decision_kind=spec.decision_kind,
            applicability_scope=spec.applicability_scope,
            operator_confirmed=spec.operator_confirmed,
            status="active",
            **self._provenance(changeset, change),
        )
        self.session.add(route)
        self.session.flush()
        if route.event_ref is not None:
            event = self.session.scalar(
                select(CanonicalEvent).where(CanonicalEvent.ref_id == route.event_ref)
            )
            if event is None:
                raise DocketError(
                    code="canonical_event_not_found",
                    message="Routing Decision event reference was not found.",
                )
            if event.lane_ref != route.lane_ref:
                raise DocketError(
                    code="lane_routing_decision_mismatch",
                    message="Routing Decision lane does not match the CanonicalEvent lane.",
                )
            event.routing_decision_ref = route.ref_id
        self._audit(
            event_type="calendar_route.decided",
            item=route,
            changeset=changeset,
            change=change,
            affected_refs=[route.ref_id, route.lane_ref],
        )
        return [route.ref_id]


class PreferenceMatcher:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active(self, *, at: datetime | None = None) -> list[Preference]:
        instant = _aware(at or utc_now())
        return list(
            self.session.scalars(
                select(Preference)
                .where(
                    Preference.status == "active",
                    or_(Preference.valid_from.is_(None), Preference.valid_from <= instant),
                    or_(Preference.valid_to.is_(None), Preference.valid_to > instant),
                )
                .order_by(Preference.priority, Preference.created_at, Preference.ref_id)
            )
        )

    @staticmethod
    def matches(
        preference: Preference,
        *,
        entity_refs: set[str],
        identity_refs: set[str],
        source_refs: set[str],
        semantic_classes: set[str],
    ) -> bool:
        if preference.target_type == "global":
            return True
        if preference.target_type == "entity":
            return preference.target_ref in entity_refs
        if preference.target_type == "identity":
            return preference.target_ref in identity_refs
        if preference.target_type == "source":
            return preference.target_ref in source_refs
        return (
            preference.target_type == "semantic_class"
            and preference.semantic_class in semantic_classes
        )

    def applicable(
        self,
        *,
        entity_refs: set[str] | None = None,
        identity_refs: set[str] | None = None,
        source_refs: set[str] | None = None,
        semantic_classes: set[str] | None = None,
        policy_kind: str | None = None,
    ) -> list[Preference]:
        return [
            item
            for item in self.active()
            if (policy_kind is None or item.policy_kind == policy_kind)
            and self.matches(
                item,
                entity_refs=entity_refs or set(),
                identity_refs=identity_refs or set(),
                source_refs=source_refs or set(),
                semantic_classes=semantic_classes or set(),
            )
        ]


class LaneRoutingService:
    """Resolve lane precedence without substituting model confidence for authority."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _lane(self, lane_ref: str) -> CalendarLane:
        lane = self.session.scalar(
            select(CalendarLane).where(
                CalendarLane.ref_id == lane_ref,
                CalendarLane.enabled.is_(True),
            )
        )
        if lane is None:
            raise DocketError(
                code="calendar_lane_unavailable",
                message="CalendarLane is missing or disabled.",
            )
        return lane

    def resolve(
        self,
        *,
        explicit_lane_ref: str | None = None,
        organization_ref: str | None = None,
        recurring_identity: str | None = None,
        semantic_classes: set[str] | None = None,
        semantic_metadata: dict[str, Any] | None = None,
        triage_policy_lane_ref: str | None = None,
    ) -> dict[str, Any]:
        if explicit_lane_ref is not None:
            lane = self._lane(explicit_lane_ref)
            return {"state": "resolved", "lane_ref": lane.ref_id, "basis": "current_utterance"}

        exact_preferences = PreferenceMatcher(self.session).applicable(
            entity_refs={organization_ref} if organization_ref else set(),
            policy_kind="calendar_route",
        )
        exact_preferences = [item for item in exact_preferences if item.target_type == "entity"]
        if exact_preferences:
            preference = exact_preferences[0]
            lane = self._lane(str(preference.policy_json["lane_ref"]))
            return {
                "state": "resolved",
                "lane_ref": lane.ref_id,
                "basis": "structured_preference",
                "basis_refs": [preference.ref_id],
            }

        if triage_policy_lane_ref is not None:
            lane = self._lane(triage_policy_lane_ref)
            return {"state": "resolved", "lane_ref": lane.ref_id, "basis": "triage_md"}

        broad_preferences = [
            item
            for item in PreferenceMatcher(self.session).applicable(
                semantic_classes=semantic_classes, policy_kind="calendar_route"
            )
            if item.target_type in {"global", "semantic_class"}
        ]
        if broad_preferences:
            preference = broad_preferences[0]
            lane = self._lane(str(preference.policy_json["lane_ref"]))
            return {
                "state": "resolved",
                "lane_ref": lane.ref_id,
                "basis": "broad_structured_preference",
                "basis_refs": [preference.ref_id],
            }

        precedent = list(
            self.session.scalars(
                select(LaneRoutingDecision)
                .where(
                    LaneRoutingDecision.status == "active",
                    LaneRoutingDecision.operator_confirmed.is_(True),
                    *(
                        [LaneRoutingDecision.organization_ref == organization_ref]
                        if organization_ref is not None
                        else [LaneRoutingDecision.recurring_identity == recurring_identity]
                        if recurring_identity is not None
                        else [LaneRoutingDecision.id.is_(None)]
                    ),
                )
                .order_by(
                    LaneRoutingDecision.decided_at.desc(),
                    LaneRoutingDecision.ref_id.desc(),
                )
                .limit(3)
            )
        )
        if len(precedent) == 3 and len({item.lane_ref for item in precedent}) == 1:
            lane = self._lane(precedent[0].lane_ref)
            return {
                "state": "resolved",
                "lane_ref": lane.ref_id,
                "basis": "historical_precedent",
                "basis_refs": [item.ref_id for item in precedent],
            }

        metadata = semantic_metadata or {}
        organization_type = metadata.get("organization_type")
        suggested = [
            lane.ref_id
            for lane in self.session.scalars(
                select(CalendarLane).where(CalendarLane.enabled.is_(True))
            )
            if organization_type is not None
            and organization_type in lane.metadata_json.get("organization_types", [])
        ]
        return {
            "state": "needs_clarification",
            "lane_ref": None,
            "basis": "semantic_metadata_advisory" if suggested else "unresolved",
            "suggested_lane_refs": suggested,
        }

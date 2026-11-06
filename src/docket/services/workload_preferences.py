"""Typed planning settings reuse canonical Preferences and exact proposals."""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.work_time import work_zone
from docket.models import Entity, NativeContext, Preference
from docket.models.base import utc_now
from docket.schemas.authority import PreferenceCreate, PreferenceModify, mutation_input_json
from docket.schemas.policy import PreferenceCreateSpec, PreferencePatchSpec
from docket.schemas.workload import (
    ProjectBudgetSpec,
    SetProjectBudget,
    SetWorkloadPreferences,
    WorkloadPreferenceSpec,
)
from docket.services.business_commands import mutation_plan
from docket.services.native_commands import NativeCommandService

WORKLOAD_COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "docket_set_workload_preferences": SetWorkloadPreferences,
    "docket_set_project_budget": SetProjectBudget,
}


def rule_key(project: str | None = None) -> str:
    owner = require_principal("read").operator_ref
    return (
        "workload:"
        + sha256_json(owner)[:24]
        + (":" + project.lower() if project else ":availability")
    )


class WorkloadPreferenceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def selected(self, project: str | None = None) -> Preference | None:
        return self.session.scalar(
            select(Preference).where(Preference.preference_key == rule_key(project))
        )

    def project(self, ref: str, *, active: bool = True) -> Entity:
        row = self.session.scalar(select(Entity).where(Entity.ref_id == ref))
        if (
            row is None
            or row.entity_kind != "project"
            or (active and row.canonical_status != "active")
        ):
            raise DocketError(
                code="workload_project_unavailable",
                message="Select an active project.",
                details={"field": "project_ref"},
            )
        return row

    def rules(self, project: str | None = None) -> list[Preference]:
        prefix = "workload:" + sha256_json(require_principal("read").operator_ref)[:24] + ":"
        statement = select(Preference).where(
            Preference.preference_key.startswith(prefix),
            Preference.status == "active",
            or_(Preference.valid_from.is_(None), Preference.valid_from <= utc_now()),
            or_(Preference.valid_to.is_(None), Preference.valid_to > utc_now()),
        )
        if project:
            statement = statement.where(
                Preference.preference_key.in_([rule_key(), rule_key(project)])
            )
        return list(self.session.scalars(statement.order_by(Preference.preference_key)))

    @staticmethod
    def parse(row: Preference) -> WorkloadPreferenceSpec | ProjectBudgetSpec:
        data = row.policy_json
        if data.get("operator_ref") != require_principal("read").operator_ref:
            raise ValueError("Planning preference belongs to another owner")
        model = (
            WorkloadPreferenceSpec
            if data.get("kind") == "workload_availability"
            else ProjectBudgetSpec
        )
        if data.get("kind") not in {"workload_availability", "workload_project_budget"}:
            raise ValueError("Unknown planning preference")
        spec = model.model_validate(data.get("values"))
        if row.policy_kind != "behavior" or (
            isinstance(spec, WorkloadPreferenceSpec) and row.target_type != "global"
        ):
            raise ValueError("Planning preference scope changed")
        if spec.timezone:
            work_zone(spec.timezone)
        if isinstance(spec, ProjectBudgetSpec) and (
            row.target_type != "entity" or row.target_ref != spec.project_ref
        ):
            raise ValueError("Project budget target changed")
        return spec


class WorkloadPreferenceCommandService(NativeCommandService):
    def perform(
        self, command: str, arguments: dict[str, Any], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        public = WORKLOAD_COMMAND_MODELS[command].model_validate(arguments)
        values = public.model_dump(mode="json", exclude_unset=True)
        rules = WorkloadPreferenceService(self.session)

        def build(context: NativeContext) -> dict[str, Any]:
            project = public.project_ref if isinstance(public, SetProjectBudget) else None
            if project:
                rules.project(project)
            previous = rules.selected(project)
            model = ProjectBudgetSpec if project else WorkloadPreferenceSpec
            data = rules.parse(previous).model_dump(mode="json") if previous else {}
            patch = {key: value for key, value in values.items() if key in model.model_fields}
            if not patch:
                raise DocketError(
                    code="workload_change_required", message="Supply a planning preference."
                )
            spec = model.model_validate({**data, **patch})
            zone = spec.timezone or self.settings.timezone
            work_zone(zone)
            content = {
                "kind": "workload_project_budget" if project else "workload_availability",
                "operator_ref": require_principal("read").operator_ref,
                "values": spec.model_dump(mode="json"),
            }
            mutation: BaseModel
            if previous:
                mutation = PreferenceModify(
                    change_id="workload-preference",
                    action="update",
                    object_type="preference",
                    object_ref=previous.ref_id,
                    affected_fields=["policy_json", "status"],
                    payload=PreferencePatchSpec(policy_json=content, status="active"),
                )
            else:
                mutation = PreferenceCreate(
                    change_id="workload-preference",
                    action="create",
                    object_type="preference",
                    affected_fields=["policy_json"],
                    create_spec=PreferenceCreateSpec(
                        preference_key=rule_key(project),
                        policy_kind="behavior",
                        target_type="entity" if project else "global",
                        target_ref=project,
                        policy_text="Reported project hour budget"
                        if project
                        else "Reported planning availability and follow-up",
                        policy_json=content,
                    ),
                )
            return mutation_plan(context, [mutation_input_json(mutation)], command)

        return self.execute(
            command=command,
            arguments=values,
            request_ref=request_ref,
            request_key=request_key,
            build=build,
        )

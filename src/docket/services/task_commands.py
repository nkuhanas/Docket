"""Compile the three flat task policy/link/resolve commands into exact proposals."""

from typing import Any

from docket.models import NativeContext
from docket.schemas.authority import (
    TaskGoogleLink,
    TaskListConfigure,
    TaskSyncResolve,
    mutation_input_json,
)
from docket.schemas.native_tasks import ConfigureTaskList, LinkGoogleTask, ResolveTaskSync
from docket.services.business_commands import mutation_plan
from docket.services.native_commands import NativeCommandService


class TaskCommandService(NativeCommandService):
    def prepare(
        self, command: ConfigureTaskList | LinkGoogleTask | ResolveTaskSync,
        request_ref: str, request_key: str,
    ) -> dict[str, Any]:
        values = command.model_dump(mode="json", exclude_unset=True)
        model: type[TaskListConfigure] | type[TaskGoogleLink] | type[TaskSyncResolve]
        if isinstance(command, ConfigureTaskList):
            kind, target, model = "configure_task_list", command.task_list_ref, TaskListConfigure
            payload = {key: value for key, value in values.items()
                       if key in {"mode", "is_default", "reminder_calendar_ref"}}
        elif isinstance(command, LinkGoogleTask):
            kind, target, model = "link_google_task", command.task_ref, TaskGoogleLink
            payload = {key: value for key, value in values.items()
                       if key in {"observation_ref", "detach"}}
        else:
            kind, target, model = "resolve_task_sync", command.conflict_ref, TaskSyncResolve
            payload = {key: value for key, value in values.items() if key in {"choose", "fields"}}

        def build(context: NativeContext) -> dict[str, Any]:
            action = model.model_validate({
                "change_id": kind,
                "object_type": {"configure_task_list": "task_list",
                                "link_google_task": "task_google_link",
                                "resolve_task_sync": "task_sync_resolution"}[kind],
                "object_ref": target, "action": "update",
                "affected_fields": list(payload), "payload": payload,
            })
            plan = mutation_plan(context, [mutation_input_json(
                action, preserve_explicit_nulls=True,
            )], kind)
            # Link and resolution actions also fence their related Task/binding,
            # whose versions the bounded task context retains explicitly.
            plan["expected_versions"].update({
                ref: version for ref, version in context.versions.items()
                if ref.startswith(("task_", "tbnd_"))
            })
            return plan

        return self.execute(command=kind, arguments=values, request_ref=request_ref,
                            request_key=request_key, build=build)

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from docket.schemas.calendar import (
    CalendarReminderDisposition,
    CalendarReminderPlanInput,
    StandaloneCalendarEventInput,
)
from docket.schemas.records import (
    DiscordId,
    DiscordRequestKey,
    RecordSourceInput,
    StrictModel,
    validate_discord_request_fields,
)

StandaloneCalendarActionType = Literal[
    "calendar_create_event",
    "calendar_update_event",
    "calendar_update_reminders",
    "calendar_cancel_event",
]
CalendarMutationScope = Literal["event", "series"]
CourseReconciliationMode = Literal["sync", "drop"]


class CreateCalendarEventProposal(StrictModel):
    kind: Literal["create"]
    event: StandaloneCalendarEventInput


class UpdateCalendarEventProposal(StrictModel):
    kind: Literal["update"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    target_scope: CalendarMutationScope = "event"
    replacement: StandaloneCalendarEventInput
    reminder_disposition: CalendarReminderDisposition = "preserve"
    reminder_plan: CalendarReminderPlanInput | None = None

    @model_validator(mode="after")
    def reminder_change_is_explicit(self) -> "UpdateCalendarEventProposal":
        if self.replacement.reminder_plan is not None:
            raise ValueError(
                "update replacement event omits reminder_plan; use reminder_disposition"
            )
        if self.reminder_disposition == "replace" and self.reminder_plan is None:
            raise ValueError("replace requires an explicit reminder_plan")
        if self.reminder_disposition != "replace" and self.reminder_plan is not None:
            raise ValueError("reminder_plan is valid only when reminder_disposition is replace")
        return self


class UpdateCalendarRemindersProposal(StrictModel):
    kind: Literal["reminders"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    target_scope: CalendarMutationScope = "event"
    reminder_plan: CalendarReminderPlanInput


class CancelCalendarEventProposal(StrictModel):
    kind: Literal["cancel"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    target_scope: CalendarMutationScope = "event"
    reason: str = Field(min_length=1, max_length=1000)


CalendarEventProposal = Annotated[
    CreateCalendarEventProposal
    | UpdateCalendarEventProposal
    | UpdateCalendarRemindersProposal
    | CancelCalendarEventProposal,
    Field(discriminator="kind"),
]


class EventEntityBindingInput(StrictModel):
    entity_id: UUID
    role: str = Field(min_length=1, max_length=128)


class InferredEventEntityRefInput(StrictModel):
    entity_id: UUID
    entity_class: Literal[
        "institution",
        "organization",
        "course",
        "person",
        "location",
        "project",
        "service",
    ]
    canonical_name: str = Field(min_length=1, max_length=512)
    role: str | None = Field(default=None, min_length=1, max_length=128)
    resolution_id: UUID | None = None
    registration_disposition: Literal["existing", "register_with_event"]


class ProposeCalendarEventInput(StrictModel):
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    proposal: CalendarEventProposal
    entity_bindings: list[EventEntityBindingInput] | None = Field(
        default=None,
        max_length=20,
    )
    context_labels: list[str] | None = Field(default=None, max_length=20)
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def request_matches_source(self) -> "ProposeCalendarEventInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        return self


class InferredCalendarEventInput(StrictModel):
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    proposal: CalendarEventProposal
    canonical_event_id: UUID
    semantic_candidate_id: UUID
    source_item_id: UUID
    entity_refs: list[InferredEventEntityRefInput] | None = Field(
        default=None,
        max_length=20,
    )
    request_key: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^gmail:[A-Za-z0-9:._/-]+$",
    )
    actor_type: Literal["hermes"] = "hermes"
    actor_id: Literal["hermes-triage"] = "hermes-triage"


class ProposeCourseReconciliationInput(StrictModel):
    record_id: UUID
    expected_record_version: int = Field(ge=1)
    mode: CourseReconciliationMode = "sync"
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    reminder_plan: CalendarReminderPlanInput | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def request_matches_source_and_mode(self) -> "ProposeCourseReconciliationInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        if self.mode == "drop" and self.reason is None:
            raise ValueError("drop requires an explicit reason")
        if self.mode == "sync" and self.reason is not None:
            raise ValueError("reason is valid only for an explicit drop")
        return self


class ProposalResult(StrictModel):
    request_id: UUID
    disposition: Literal["proposed", "matched_existing", "replayed_request"]
    queue_item_id: UUID
    action_id: UUID
    action_revision_id: UUID
    approval_id: UUID
    short_code: str
    expires_at: datetime
    preview: dict[str, Any]
    projection_status: Literal["pending"] = "pending"


class DirectExecutionResult(StrictModel):
    request_id: UUID
    disposition: Literal["execution_queued", "replayed_request"]
    authority: Literal["explicit_user"] = "explicit_user"
    queue_item_id: UUID
    action_id: UUID
    action_revision_id: UUID
    operation_id: UUID
    operation_status: Literal["pending", "running", "succeeded"]
    presentation: Literal["suppressed"] = "suppressed"
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class AccountResult(StrictModel):
    account_id: UUID
    provider: Literal["google"]
    external_account_id: str
    display_name: str | None
    email_address: str | None
    capabilities: list[str]

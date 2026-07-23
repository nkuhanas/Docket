from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscordContext(InternalModel):
    request_id: UUID
    discord_interaction_id: str = Field(min_length=1, max_length=255)
    discord_user_id: str = Field(min_length=1, max_length=64)
    guild_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    parent_channel_id: str | None = Field(default=None, min_length=1, max_length=64)
    projection_id: UUID | None = None
    message_id: str = Field(min_length=1, max_length=64)
    responded_at: datetime


class ApprovalResponse(DiscordContext):
    approval_id: UUID | None = None
    approval_token: str | None = Field(default=None, max_length=100)
    short_code: str | None = Field(default=None, min_length=8, max_length=32)
    decision: Literal["approve", "reject"]

    @model_validator(mode="after")
    def exactly_one_reference(self) -> "ApprovalResponse":
        if (self.approval_token is None) == (self.short_code is None):
            raise ValueError("Exactly one of approval_token and short_code is required")
        if self.approval_token is not None and self.approval_id is None:
            raise ValueError("approval_id is required with approval_token")
        if self.approval_token is not None and (
            self.parent_channel_id is None or self.projection_id is None
        ):
            raise ValueError("parent_channel_id and projection_id are required with approval_token")
        if self.short_code is not None and (
            self.approval_id is not None
            or self.parent_channel_id is not None
            or self.projection_id is not None
        ):
            raise ValueError(
                "fallback responses omit approval_id, parent_channel_id, and projection_id"
            )
        return self


class LocalActionResponse(DiscordContext):
    action_revision_id: UUID
    action_token: str = Field(min_length=1, max_length=100)
    transition: Literal[
        "local_action",
        "proposal_refresh",
        "proposal_edit",
        "proposal_field_change",
        "proposal_snooze",
        "proposal_review_page",
    ] = "local_action"
    field: Literal["priority", "reminder_preset"] | None = None
    value: str | None = Field(default=None, min_length=1, max_length=64)
    modal_values: dict[str, str] | None = None

    @model_validator(mode="after")
    def require_projection_context(self) -> "LocalActionResponse":
        if self.parent_channel_id is None or self.projection_id is None:
            raise ValueError("parent_channel_id and projection_id are required")
        if self.transition == "local_action":
            if self.field is not None or self.value is not None or self.modal_values is not None:
                raise ValueError("ordinary local actions omit proposal-control values")
        elif self.transition == "proposal_field_change":
            if self.field is None or self.value is None or self.modal_values is not None:
                raise ValueError("proposal field changes require exactly field and value")
        elif self.transition == "proposal_edit":
            if (
                self.modal_values is None
                or self.field not in {None, "reminder_preset"}
                or self.value is not None
            ):
                raise ValueError(
                    "proposal edits require bounded modal_values and an optional "
                    "allowlisted field binding"
                )
            if not 1 <= len(self.modal_values) <= 5:
                raise ValueError("proposal edits accept from one through five modal fields")
            if any(
                not key
                or len(key) > 64
                or not value
                or len(value) > 1000
                for key, value in self.modal_values.items()
            ):
                raise ValueError("proposal edit modal values exceed their bounds")
        elif self.field is not None or self.value is not None or self.modal_values is not None:
            raise ValueError("this proposal control does not accept field values")
        return self

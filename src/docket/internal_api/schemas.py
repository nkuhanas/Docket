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

    @model_validator(mode="after")
    def require_projection_context(self) -> "LocalActionResponse":
        if self.parent_channel_id is None or self.projection_id is None:
            raise ValueError("parent_channel_id and projection_id are required")
        return self

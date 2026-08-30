from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpTraceCallUpdate(InternalModel):
    call_id: str = Field(min_length=1, max_length=255)
    ordinal: int = Field(ge=1, le=100)
    tool_name: str = Field(min_length=1, max_length=128)
    transport_state: Literal["running", "completed", "failed", "timed_out"]
    elapsed_ms: int = Field(default=0, ge=0, le=600_000)
    disposition: str | None = Field(default=None, min_length=1, max_length=64)
    transport_error_code: str | None = Field(default=None, min_length=1, max_length=64)
    received_argument_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    argument_preview: str = Field(default="{}", min_length=2, max_length=768)

    @model_validator(mode="after")
    def validate_terminal_details(self) -> "McpTraceCallUpdate":
        if self.transport_state == "running" and (
            self.elapsed_ms != 0
            or self.disposition is not None
            or self.transport_error_code is not None
        ):
            raise ValueError("running trace calls omit terminal details")
        if self.transport_state == "completed" and self.transport_error_code is not None:
            raise ValueError("completed transport omits transport_error_code")
        if self.transport_state in {"failed", "timed_out"} and self.disposition is not None:
            raise ValueError("failed trace calls omit disposition")
        return self


class McpTraceUpdate(InternalModel):
    request_id: UUID
    guild_id: str = Field(min_length=1, max_length=64)
    source_channel_id: str = Field(min_length=1, max_length=64)
    source_message_id: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=64)
    tool_contract_version: str = Field(min_length=1, max_length=128)
    tool_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    caller_profile: Literal["interactive"]
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    updated_at: datetime
    turn_status: Literal["running", "completed", "failed", "interrupted"] = "running"
    call: McpTraceCallUpdate | None = None

    @model_validator(mode="after")
    def require_update(self) -> "McpTraceUpdate":
        if self.call is None and self.turn_status == "running":
            raise ValueError("a running trace update requires a call")
        return self


DiscordSnowflake = str


class AttachmentManifest(InternalModel):
    transport_attachment_ref: str = Field(min_length=1, max_length=512)
    filename: str | None = Field(default=None, max_length=512)
    media_type: str | None = Field(default=None, max_length=255)
    byte_size: int | None = Field(default=None, ge=0)
    received_at: datetime


class OperatorUtteranceCapture(InternalModel):
    request_id: UUID
    guild_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    channel_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    parent_channel_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    message_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    actor_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    reply_to_message_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    verbatim_text: str = Field(max_length=100_000)
    request_key: str = Field(min_length=8, max_length=512)
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    attachments: list["AttachmentManifest"] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def request_key_matches_source(self) -> "OperatorUtteranceCapture":
        expected = f"discord:{self.guild_id}:{self.channel_id}:{self.message_id}:0"
        if self.request_key != expected:
            raise ValueError("request_key must match the Discord message binding")
        return self


class SemanticOptionSelection(InternalModel):
    request_id: UUID
    discord_interaction_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    discord_user_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    guild_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    channel_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    parent_channel_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    message_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    option_token: str = Field(min_length=40, max_length=100)
    responded_at: datetime
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    resume_authorized_execution: bool = False


class GatewayLifetimeRegister(InternalModel):
    request_id: UUID
    registration_key: UUID
    instance_kind: Literal["hermes_discord_gateway", "discord_ingress"]


class GatewayLifetimeHeartbeat(InternalModel):
    request_id: UUID
    gateway_instance_ref: str = Field(pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    status: Literal["active", "draining"] = "active"


class GatewayLifetimeShutdown(InternalModel):
    request_id: UUID
    gateway_instance_ref: str = Field(pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")


class ExecutionLeaseComplete(InternalModel):
    request_id: UUID
    completion_token: str = Field(pattern=r"^[0-9a-f]{32}$")
    deferred_ingress_ref: str | None = Field(default=None, pattern=r"^ing_[0-9A-HJKMNP-TV-Z]{26}$")
    gateway_instance_ref: str = Field(pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    outcome: Literal["completed", "rejected", "failed"]
    error_code: str | None = Field(default=None, min_length=1, max_length=128)


class AgentResponseCapture(InternalModel):
    request_id: UUID
    guild_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    channel_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    parent_channel_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    source_message_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    actor_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    turn_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    verbatim_text: str = Field(min_length=1, max_length=100_000)
    generated_at: datetime
    trace_ref: str = Field(pattern=r"^trace_[0-9A-HJKMNP-TV-Z]{26}$")
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")
    finalize_intent_turn: bool = True


class AgentTurnNoResponse(InternalModel):
    request_id: UUID
    guild_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    channel_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    parent_channel_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    source_message_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    actor_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    turn_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    trace_ref: str = Field(pattern=r"^trace_[0-9A-HJKMNP-TV-Z]{26}$")
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")


class AgentResponseDeliveryUpdate(InternalModel):
    request_id: UUID
    response_ref: str = Field(pattern=r"^rsp_[0-9A-HJKMNP-TV-Z]{26}$")
    guild_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    channel_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    parent_channel_id: DiscordSnowflake | None = Field(default=None, pattern=r"^[0-9]{17,20}$")
    source_message_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    actor_id: DiscordSnowflake = Field(pattern=r"^[0-9]{17,20}$")
    outcome: Literal["delivered", "failed"]
    completed_at: datetime
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    gateway_instance_ref: str | None = Field(default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$")

    @model_validator(mode="after")
    def delivery_error_matches_outcome(self) -> "AgentResponseDeliveryUpdate":
        if self.outcome == "delivered" and self.error_code is not None:
            raise ValueError("delivered responses omit error_code")
        if self.outcome == "failed" and self.error_code is None:
            raise ValueError("failed responses require error_code")
        return self


class SpecificationSignoffCapture(InternalModel):
    request_id: UUID
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    document_ref: str = Field(pattern=r"^ONT-DELTA-[A-Z0-9-]+$", max_length=255)
    frozen_artifact_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class ProductionResetAuthorizationCapture(InternalModel):
    request_id: UUID
    utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    document_ref: str = Field(pattern=r"^ONT-DELTA-[A-Z0-9-]+$", max_length=255)
    frozen_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_backup_ref: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    verified_backup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_revision: str = Field(pattern=r"^[0-9a-f]{40}$")

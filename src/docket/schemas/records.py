from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecordType = Literal["term", "generic"]
_DISCORD_REQUEST_KEY_PATTERN = (
    r"^discord:[0-9]{17,20}:[0-9]{17,20}:[0-9]{17,20}:(0|[1-9][0-9]*)$"
)
_DISCORD_ID_PATTERN = r"^[0-9]{17,20}$"
DiscordId = Annotated[str, Field(pattern=_DISCORD_ID_PATTERN)]
DiscordRequestKey = Annotated[
    str,
    Field(min_length=8, max_length=512, pattern=_DISCORD_REQUEST_KEY_PATTERN),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TermIdentity(StrictModel):
    institution: str = Field(min_length=1, max_length=255)
    term_name: str = Field(min_length=1, max_length=255)


class GenericIdentity(StrictModel):
    key: str = Field(min_length=1, max_length=255)


class GenericRecordData(BaseModel):
    model_config = ConfigDict(extra="allow")


class DiscordSourceMetadata(StrictModel):
    guild_id: DiscordId
    channel_id: DiscordId
    message_id: DiscordId
    user_id: DiscordId
    intent_index: int = Field(ge=0)


class RecordSourceInput(StrictModel):
    source_type: Literal["discord_message"]
    source_object_id: DiscordId
    source_version: str | None = Field(default=None, max_length=255)
    metadata: DiscordSourceMetadata


class TermData(StrictModel):
    institution: str = Field(min_length=1, max_length=255)
    term_name: str = Field(min_length=1, max_length=255)
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = "America/Los_Angeles"
    notes: str | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "TermData":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class RememberRecordInput(StrictModel):
    record_type: RecordType
    canonical_identity: TermIdentity | GenericIdentity
    title: str = Field(min_length=1, max_length=512)
    data: TermData | GenericRecordData
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def record_and_source_shapes_match(self) -> "RememberRecordInput":
        if self.record_type == "term":
            if not isinstance(self.canonical_identity, TermIdentity) or not isinstance(
                self.data, TermData
            ):
                raise ValueError("term records require TermIdentity and TermData")
        elif not isinstance(self.canonical_identity, GenericIdentity) or not isinstance(
            self.data, GenericRecordData
        ):
            raise ValueError("generic records require GenericIdentity and GenericRecordData")

        source = self.source
        metadata = source.metadata
        if source.source_object_id != metadata.message_id:
            raise ValueError("source_object_id must equal metadata.message_id")
        if self.actor_id != metadata.user_id:
            raise ValueError("actor_id must equal metadata.user_id")
        expected_key = (
            f"discord:{metadata.guild_id}:{metadata.channel_id}:"
            f"{metadata.message_id}:{metadata.intent_index}"
        )
        if self.request_key != expected_key:
            raise ValueError("request_key must match the verified Discord source metadata")
        return self


class UpdateRecordInput(StrictModel):
    record_id: UUID
    expected_version: int = Field(ge=1)
    data: dict[str, Any]
    request_key: str = Field(min_length=8, max_length=512)
    reason: str = Field(min_length=1, max_length=1000)
    actor_type: Literal["user", "hermes", "system"] = "hermes"
    actor_id: str | None = Field(default=None, max_length=255)


class ArchiveRecordInput(StrictModel):
    record_id: UUID
    expected_version: int = Field(ge=1)
    request_key: str = Field(min_length=8, max_length=512)
    reason: str = Field(min_length=1, max_length=1000)
    actor_type: Literal["user", "hermes", "system"] = "hermes"
    actor_id: str | None = Field(default=None, max_length=255)


class RecordResult(StrictModel):
    record_id: UUID
    version: int
    disposition: Literal["created", "matched_existing", "replayed_request", "updated", "archived"]
    request_id: UUID

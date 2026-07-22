from datetime import date, time
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecordType = Literal["term", "course", "generic"]
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


class CourseIdentity(StrictModel):
    term_record_id: UUID
    course_code: str = Field(min_length=1, max_length=64)
    section: str | None = Field(default=None, min_length=1, max_length=64)


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


def validate_discord_request_fields(
    request_key: str, source: RecordSourceInput, actor_id: str
) -> None:
    metadata = source.metadata
    if source.source_object_id != metadata.message_id:
        raise ValueError("source_object_id must equal metadata.message_id")
    if actor_id != metadata.user_id:
        raise ValueError("actor_id must equal metadata.user_id")
    expected_key = (
        f"discord:{metadata.guild_id}:{metadata.channel_id}:"
        f"{metadata.message_id}:{metadata.intent_index}"
    )
    if request_key != expected_key:
        raise ValueError("request_key must match the verified Discord source metadata")


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


MeetingDay = Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MeetingId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class CourseMeeting(StrictModel):
    meeting_type: str = Field(min_length=1, max_length=64)
    days: list[MeetingDay] = Field(min_length=1, max_length=7)
    start_time: time | None = None
    end_time: time | None = None
    location: str | None = Field(default=None, max_length=512)
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("days")
    @classmethod
    def days_must_be_unique(cls, value: list[MeetingDay]) -> list[MeetingDay]:
        if len(value) != len(set(value)):
            raise ValueError("meeting days must be unique")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> "CourseMeeting":
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class CourseData(StrictModel):
    term_record_id: UUID
    course_code: str = Field(min_length=1, max_length=64)
    course_title: str | None = Field(default=None, max_length=512)
    section: str | None = Field(default=None, min_length=1, max_length=64)
    instructor: str | None = Field(default=None, max_length=255)
    meetings: dict[MeetingId, CourseMeeting] = Field(default_factory=dict, max_length=32)
    notes: str | None = None


class RememberRecordInput(StrictModel):
    record_type: RecordType
    canonical_identity: TermIdentity | CourseIdentity | GenericIdentity
    title: str = Field(min_length=1, max_length=512)
    data: TermData | CourseData | GenericRecordData
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
        elif self.record_type == "course":
            if not isinstance(self.canonical_identity, CourseIdentity) or not isinstance(
                self.data, CourseData
            ):
                raise ValueError("course records require CourseIdentity and CourseData")
        elif not isinstance(self.canonical_identity, GenericIdentity) or not isinstance(
            self.data, GenericRecordData
        ):
            raise ValueError("generic records require GenericIdentity and GenericRecordData")

        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
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

from datetime import date
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from docket.schemas.triage import EntityClass

EntityResolutionState = Literal["resolved", "unresolved", "ambiguous", "provisional"]
EntityStatus = Literal["active", "provisional", "merged", "archived"]
EntitySearchStatus = Literal["active", "provisional", "active_or_provisional", "all"]
EntityRelationDirection = Literal["any", "subject", "object"]
EntityPredicate = Literal[
    "works_for",
    "member_of",
    "affiliated_with",
    "advises",
    "instructs",
    "reports_to",
    "collaborates_with",
    "knows",
    "friend_of",
    "classmate_of",
    "leads",
    "participates_in",
    "located_at",
    "uses",
    "supports",
]
EntityAttributeKey = Literal[
    "preferred_name",
    "pronouns",
    "email_addresses",
    "phone_numbers",
    "job_title",
    "department",
    "timezone",
    "preferred_contact_method",
    "discord_user_ids",
    "website",
    "description",
    "notes",
    "context",
    "context_labels",
    "external_refs",
    "organization_type",
    "address",
    "building",
    "room",
    "is_operator",
]

ShortText = Annotated[str, Field(min_length=1, max_length=256)]
LongText = Annotated[str, Field(min_length=1, max_length=4000)]
ContactValue = Annotated[str, Field(min_length=1, max_length=512)]
DiscordUserId = Annotated[str, Field(pattern=r"^[0-9]{17,20}$")]
EntityName = Annotated[str, Field(min_length=1, max_length=512)]


class EntityAttributes(BaseModel):
    """Validated metadata shared by Docket's deliberately small entity classes."""

    model_config = ConfigDict(extra="forbid")

    preferred_name: ShortText | None = None
    pronouns: ShortText | None = None
    email_addresses: list[ContactValue] | None = Field(default=None, max_length=20)
    phone_numbers: list[ContactValue] | None = Field(default=None, max_length=20)
    job_title: ShortText | None = None
    department: ShortText | None = None
    timezone: ShortText | None = None
    preferred_contact_method: Literal["email", "phone", "discord", "other"] | None = None
    discord_user_ids: list[DiscordUserId] | None = Field(default=None, max_length=10)
    website: ContactValue | None = None
    description: LongText | None = None
    notes: LongText | None = None
    context: ShortText | None = None
    context_labels: list[ShortText] | None = Field(default=None, max_length=30)
    external_refs: dict[ShortText, ContactValue] | None = Field(default=None, max_length=30)
    organization_type: ShortText | None = None
    address: ContactValue | None = None
    building: ShortText | None = None
    room: ShortText | None = None
    is_operator: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value

    @field_validator(
        "email_addresses",
        "phone_numbers",
        "discord_user_ids",
        "context_labels",
    )
    @classmethod
    def _deduplicate_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value))


class EntityRelationAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ShortText | None = None
    context: ShortText | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: LongText | None = None
    primary: bool | None = None

    @model_validator(mode="after")
    def _ordered_dates(self) -> "EntityRelationAttributes":
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date")
        return self


class EntityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    entity_class: EntityClass
    canonical_name: str
    status: EntityStatus
    attributes: EntityAttributes
    authority: Literal["explicit_user", "canonical", "inferred"]
    version: int
    merged_into_id: UUID | None = None


class EntityAliasResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias_id: UUID
    alias: str
    authority: Literal["explicit_user", "canonical", "inferred"]
    confidence: float


class EntityRelationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    predicate: EntityPredicate
    subject: EntityResult
    object: EntityResult
    attributes: EntityRelationAttributes
    authority: Literal["explicit_user", "canonical", "inferred"]
    version: int


class EntitySnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: EntityResult
    aliases: list[EntityAliasResult] = Field(default_factory=list)
    relationships: list[EntityRelationResult] = Field(default_factory=list)


class EntityResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_id: UUID
    entity_class: EntityClass
    mention: str
    state: EntityResolutionState
    resolved_entity: EntityResult | None = None
    candidates: list[EntityResult] = Field(default_factory=list)

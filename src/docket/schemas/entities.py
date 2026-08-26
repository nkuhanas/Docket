from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from docket.schemas.triage import EntityClass

EntityResolutionState = Literal["resolved", "unresolved", "ambiguous", "provisional"]


class EntityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    entity_class: EntityClass
    canonical_name: str
    status: Literal["active", "provisional", "merged", "archived"]
    attributes: dict[str, Any]
    authority: Literal["explicit_user", "canonical", "inferred"]
    version: int
    merged_into_id: UUID | None = None


class EntityResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_id: UUID
    entity_class: EntityClass
    mention: str
    state: EntityResolutionState
    resolved_entity: EntityResult | None = None
    candidates: list[EntityResult] = Field(default_factory=list)

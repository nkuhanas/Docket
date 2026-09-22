"""Shared evidence input, used by staged work and persisted clarification choices."""

import json
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from docket.schemas.common import StrictModel


def structural_locator(value: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"body", "content", "excerpt", "quote", "raw", "text", "transcript"}

    def visit(item: Any, depth: int = 0) -> int:
        if depth > 8:
            raise ValueError("source_fragment_locator exceeds maximum nesting depth")
        if isinstance(item, dict):
            if any(str(key).casefold() in forbidden for key in item):
                raise ValueError("source_fragment_locator contains copied source content")
            return 1 + sum(visit(nested, depth + 1) for nested in item.values())
        if isinstance(item, list):
            return 1 + sum(visit(nested, depth + 1) for nested in item)
        if isinstance(item, str) and len(item.encode("utf-8")) > 256:
            raise ValueError("source_fragment_locator coordinate is too large")
        return 1

    if visit(value) > 100:
        raise ValueError("source_fragment_locator contains too many coordinates")
    if len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 2048:
        raise ValueError("source_fragment_locator exceeds 2048 bytes")
    return value


class FieldEvidenceTarget(StrictModel):
    change_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    field_path: str = Field(
        pattern=r"^(create_spec|payload)(\.[a-z][a-z0-9_]*){1,4}$",
        description="Exact existing text field, e.g. create_spec.event_spec.location.",
    )
    match: Literal["exact", "prefix"] = "exact"

    @field_validator("field_path")
    @classmethod
    def descriptive_field_only(cls, value: str) -> str:
        if value.split(".")[-1] not in {
            "location",
            "description",
            "notes",
            "title",
            "display_name",
            "name",
            "website",
        }:
            raise ValueError("field evidence supports descriptive text, not time/authority/routing")
        return value


class FieldEvidenceInput(StrictModel):
    """A fallible reading of a source, not an authority grant or a schedule row."""

    source_ref: Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
    source_fragment_locator: dict[str, Any]
    source_fragment_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    extractor_identifier: str = Field(min_length=1, max_length=255)
    extractor_version: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=4000)
    targets: list[FieldEvidenceTarget] = Field(min_length=1, max_length=100)

    @field_validator("source_fragment_locator")
    @classmethod
    def structural_locator(cls, value: dict[str, Any]) -> dict[str, Any]:
        return structural_locator(value)

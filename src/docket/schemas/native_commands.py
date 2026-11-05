"""Small business contracts; internal compiler vocabulary stays in services."""

from typing import Annotated

from pydantic import Field

from docket.schemas.common import StrictModel

ContextId = Annotated[str, Field(pattern=r"^nctx_[0-9A-HJKMNP-TV-Z]{26}$")]
OperationId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Stable identity for this command and unchanged retries.",
    ),
]
CalendarRef = Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]


class CreateEvent(StrictModel):
    context_id: ContextId
    operation_id: OperationId
    title: str = Field(min_length=1, max_length=512)
    start: str = Field(max_length=64, description="ISO timestamp, preferably including UTC offset.")
    end: str = Field(max_length=64, description="ISO timestamp after start.")
    timezone: str = Field(max_length=128, description="IANA zone, e.g. America/Los_Angeles.")
    calendar_ref: CalendarRef
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)

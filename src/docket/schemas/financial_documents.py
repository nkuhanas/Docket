"""Small named timesheet inputs; retained calculation/claim graphs stay private."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field

from docket.schemas.common import StrictModel
from docket.schemas.compensation import ComponentKey
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EngagementRef

DocumentRef = Annotated[str, Field(pattern=r"^fdoc_[0-9A-HJKMNP-TV-Z]{26}$")]
EvaluationRef = Annotated[str, Field(pattern=r"^ceval_[0-9A-HJKMNP-TV-Z]{26}$")]


class PrepareTimesheet(CommandBase):
    engagement_ref: EngagementRef
    date_from: date | None = None
    date_to: date | None = Field(
        default=None, description="Exclusive. Omit both for the current reporting period."
    )
    timezone: str | None = Field(default=None, max_length=128)
    component_key: ComponentKey = "base"
    amends_ref: DocumentRef | None = None


class UpdateTimesheet(CommandBase):
    timesheet_ref: DocumentRef
    status: Literal["verified", "submitted", "approved", "rejected", "cancelled"]
    note: str | None = Field(default=None, max_length=4000)
    observed_at: datetime | None = None


class RefreshTimesheet(CommandBase):
    timesheet_ref: DocumentRef


class ReadDocument(StrictModel):
    ref: Annotated[str, Field(pattern=r"^(fdoc|fdrev)_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    format: Literal["summary", "json", "csv", "markdown"] = "summary"
    cursor: str | None = Field(default=None, max_length=512)
    max_bytes: int = Field(default=4096, ge=256, le=8192)


class TimesheetSpec(StrictModel):
    engagement_ref: EngagementRef
    component_key: ComponentKey = "base"
    evaluation_ref: EvaluationRef
    amends_ref: DocumentRef | None = None


class TimesheetStatusSpec(StrictModel):
    status: Literal["verified", "submitted", "approved", "rejected", "cancelled"]
    note: str | None = None
    observed_at: datetime | None = None


FINANCIAL_TOOL_PURPOSES = {
    "docket_read_document": "Read an owned formal document or exact revision; "
    "summary, JSON, CSV or usable Markdown. Follow cursor until complete. "
    "Exports retain the original basis and create no submission, delivery or payment.",
    "docket_prepare_timesheet": "Prepare a formal timesheet for an engagement and period. "
    "Reserves available work quantities once; missing rates remain explicit. "
    "Omitted dates use configured current period or current week. Does not submit or pay.",
    "docket_update_timesheet": "Verify/cancel a timesheet or record reported manual submission, "
    "approval or rejection. Submitted snapshots stay immutable; payment is independent. "
    "No external submission or provider effect.",
    "docket_refresh_timesheet": "Regenerate a draft timesheet from current work and pay terms. "
    "Replaces its reservations atomically and retains earlier revisions. "
    "Submitted reports need a new amendment; they cannot be refreshed.",
}

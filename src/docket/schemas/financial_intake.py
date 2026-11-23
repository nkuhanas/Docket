from decimal import Decimal
from typing import Annotated

from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

from docket.schemas.common import StrictModel
from docket.schemas.expenses import ExpenseRef
from docket.schemas.funding import ActivityRef, InstrumentHint, InstrumentRef
from docket.schemas.native_commands import ContextId, OperationId


class HostFile(StrictModel):
    file_id: str = Field(min_length=1, max_length=256)
    download_url: str = Field(min_length=1, max_length=8192, repr=False)
    mime_type: str | SkipJsonSchema[None] = Field(default=None, max_length=120)
    file_name: str | SkipJsonSchema[None] = Field(default=None, max_length=256)


class SubmitEvidence(StrictModel):
    context_id: ContextId
    operation_id: OperationId
    file: HostFile
    business_use_percent: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=9, decimal_places=6
    )
    write_off_intent: bool | None = None
    reported_instrument: InstrumentHint | None = None


CandidateRef = Annotated[str, Field(pattern=r"^fcand_[0-9A-HJKMNP-TV-Z]{26}$")]


class ResolveEvidence(StrictModel):
    activity_ref: ActivityRef | None = None
    existing_expense_ref: ExpenseRef | None = None
    instrument_ref: InstrumentRef | None = None

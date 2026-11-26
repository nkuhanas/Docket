from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, WithJsonSchema, model_validator
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
    evidence_role: Literal["income_document", "statement"] | None = Field(
        default=None,
        description="Retain pay or statement evidence without purchase extraction.",
    )
    file: Annotated[HostFile, WithJsonSchema(HostFile.model_json_schema())]
    business_use_percent: Annotated[
        Decimal | None, WithJsonSchema({"type": "number", "minimum": 0, "maximum": 100})
    ] = Field(
        default=None,
        ge=0,
        le=100,
        max_digits=9,
        decimal_places=6,
        description="Reported business use from 0 to 100. A positive share is intended for claim.",
    )
    write_off_intent: bool | SkipJsonSchema[None] = Field(
        default=None, description="Optional explicit override, e.g. false for 'do not claim'."
    )
    reported_instrument: InstrumentHint | None = None


CandidateRef = Annotated[str, Field(pattern=r"^fcand_[0-9A-HJKMNP-TV-Z]{26}$")]


class ResolveEvidence(StrictModel):
    decision: Literal["ask", "answer", "dismiss", "reopen"] | None = None
    note: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Exact question, answer, reason or common purchase evidence for a link.",
    )
    activity_ref: ActivityRef | None = None
    existing_expense_ref: ExpenseRef | None = None
    instrument_ref: InstrumentRef | None = None

    @model_validator(mode="after")
    def decision_fields(self) -> "ResolveEvidence":
        if self.decision and (
            self.activity_ref or self.instrument_ref or self.existing_expense_ref
        ):
            raise ValueError("Use one review decision or one link operation.")
        if (self.decision or self.existing_expense_ref) and not (self.note or "").strip():
            raise ValueError("Supply the question, answer or decision basis in note.")
        return self

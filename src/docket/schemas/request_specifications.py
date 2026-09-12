"""Internal, versioned request interpretations; recording is not verification.

The proposal's integrity digest is deliberately not an authority hash. Evidence
validation must produce its own immutable result before a proposal can be used
as the authority oracle for a repair.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from docket.schemas.assembly import AssemblyAuthorityScopeInput, NormalizedEntryInput
from docket.schemas.authority import CanonicalChangeInput, UtteranceRef
from docket.schemas.common import StrictModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RequestUtteranceBinding(StrictModel):
    utterance_ref: UtteranceRef
    content_hash: Digest


class RequestSourceBinding(StrictModel):
    source_ref: Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
    source_manifest_hash: Digest | None = None
    attachment_content_hash: Digest | None = None
    evidence_state: Literal["source_recorded", "attachment_recorded", "missing"]


class RequestEntryInterpretationInput(StrictModel):
    """Docket-recorded initial interpretation, not a model-facing authority assertion."""

    schema_version: Literal[1] = 1
    interpretation_state: Literal["recorded_interpretation"] = "recorded_interpretation"
    originating_utterances: list[RequestUtteranceBinding] = Field(min_length=1, max_length=100)
    source_binding: RequestSourceBinding
    entry: NormalizedEntryInput


class RequestSpecificationProposal(StrictModel):
    schema_version: Literal[1] = 1
    interpretation_state: Literal["pending_evidence_validation"] = "pending_evidence_validation"
    originating_utterances: list[RequestUtteranceBinding] = Field(min_length=1, max_length=100)
    source_bindings: list[RequestSourceBinding] = Field(default_factory=list, max_length=250)
    assembly_boundary: AssemblyAuthorityScopeInput
    normalized_entries: list[NormalizedEntryInput] = Field(default_factory=list, max_length=250)
    direct_actions: list[CanonicalChangeInput] = Field(default_factory=list, max_length=1000)

    # Mechanical support actions, expected versions, provider intents, and
    # compiler pins belong to ChangeSetRevision, not this semantic proposal.
    # Model-provided unresolved values remain visible in the typed entries or
    # boundary; no "verified" flag can be supplied by the model.

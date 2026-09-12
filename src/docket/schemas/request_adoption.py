"""Evidence of effect-preserving migration, not a new authority grant."""

from typing import Literal

from pydantic import Field

from docket.schemas.assembly import AssemblyAuthorityScopeInput
from docket.schemas.common import StrictModel
from docket.schemas.request_specifications import (
    Digest,
    RequestSourceBinding,
    RequestUtteranceBinding,
)


class RequestAdoptionProof(StrictModel):
    format_version: Literal[1] = 1
    comparison_rule: Literal["current_typed_scope_and_provider_identity"] = (
        "current_typed_scope_and_provider_identity"
    )
    original_authority_scope_hash: Digest
    original_parameter_hash: Digest
    original_binding_hash: Digest
    provider_effect_hash: Digest
    origin_utterances: list[RequestUtteranceBinding] = Field(min_length=1, max_length=100)
    source_bindings: list[RequestSourceBinding] = Field(default_factory=list, max_length=250)
    assembly_boundary: AssemblyAuthorityScopeInput

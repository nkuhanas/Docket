from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from docket.domain.public_refs import is_public_ref, parse_public_ref


def _validate_public_ref(value: str) -> str:
    if not is_public_ref(value):
        raise ValueError("value must be a typed Docket public reference")
    return value


PublicRef = Annotated[
    str,
    Field(min_length=29, max_length=35),
    AfterValidator(_validate_public_ref),
]

HistoryObjectType = Literal[
    "operator_utterance",
    "agent_response",
    "interpreted_statement",
    "decision",
    "intent_session",
    "intent_turn",
    "changeset",
    "conflict",
    "entity",
    "identity_handle",
    "affiliation",
    "relationship",
    "fact",
    "interaction",
    "calendar_lane",
    "preference",
    "lane_routing_decision",
    "canonical_event",
    "source",
    "daily_brief",
    "triage_run",
    "context_packet",
    "attention_case",
    "attention_case_revision",
    "case_item",
    "operation",
    "tool_invocation",
    "audit_event",
    "runtime_log_entry",
    "triage_brief_entry",
    "provider_identity",
]

_PROVENANCE_PREFIXES = frozenset(
    {
        "utt",
        "src",
        "stm",
        "dec",
        "cnf",
        "chg",
        "ent",
        "idn",
        "aff",
        "rel",
        "fact",
        "int",
        "pref",
        "lane",
        "route",
        "evt",
        "rsp",
        "tri",
        "case",
        "caserev",
        "item",
        "brief",
        "ctx",
        "ses",
        "turn",
        "sreq",
        "call",
        "aud",
        "op",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_refs(values: list[str], *, provenance_only: bool = False) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("public reference lists must not contain duplicates")
    for value in values:
        if not is_public_ref(value):
            raise ValueError("value must be a typed Docket public reference")
        prefix, _payload = parse_public_ref(value)
        if provenance_only and prefix not in _PROVENANCE_PREFIXES:
            raise ValueError("value is not an allowed ProvenanceRef")
    return values


_validate_refs = validate_refs

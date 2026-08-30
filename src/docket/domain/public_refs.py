from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass
from types import MappingProxyType

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PUBLIC_REF = re.compile(r"^(?P<prefix>[a-z][a-z0-9]{1,7})_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})$")

@dataclass(frozen=True, slots=True)
class PublicRefDefinition:
    prefix: str
    canonical_type: str


_DEFINITIONS = (
    PublicRefDefinition("utt", "OperatorUtterance"),
    PublicRefDefinition("rsp", "AgentResponse"),
    PublicRefDefinition("stm", "InterpretedStatement"),
    PublicRefDefinition("src", "Source"),
    PublicRefDefinition("acct", "ProviderAccount"),
    PublicRefDefinition("ent", "Entity"),
    PublicRefDefinition("item", "Item"),
    PublicRefDefinition("task", "Task"),
    PublicRefDefinition("time", "TemporalBinding"),
    PublicRefDefinition("evt", "CanonicalEvent"),
    PublicRefDefinition("idn", "IdentityHandle"),
    PublicRefDefinition("aff", "Affiliation"),
    PublicRefDefinition("rel", "Relationship"),
    PublicRefDefinition("fact", "Fact"),
    PublicRefDefinition("int", "Interaction"),
    PublicRefDefinition("pref", "Preference"),
    PublicRefDefinition("lane", "CalendarLane"),
    PublicRefDefinition("route", "LaneRoutingDecision"),
    PublicRefDefinition("tproj", "TemporalCalendarProjection"),
    PublicRefDefinition("rem", "ReminderPlan"),
    PublicRefDefinition("tri", "TriageRun"),
    PublicRefDefinition("ctx", "ContextPacket"),
    PublicRefDefinition("case", "AttentionCase"),
    PublicRefDefinition("caserev", "AttentionCaseRevision"),
    PublicRefDefinition("citem", "CaseItem"),
    PublicRefDefinition("bentry", "BriefEntry"),
    PublicRefDefinition("brief", "DailyBrief"),
    PublicRefDefinition("ses", "IntentSession"),
    PublicRefDefinition("turn", "IntentTurn"),
    PublicRefDefinition("sreq", "SemanticRequest"),
    PublicRefDefinition("sattempt", "SemanticRequestAttempt"),
    PublicRefDefinition("chg", "ChangeSet"),
    PublicRefDefinition("dec", "Decision"),
    PublicRefDefinition("conf", "Conflict"),
    PublicRefDefinition("proj", "OperatorProjection"),
    PublicRefDefinition("opt", "PersistedSemanticOption"),
    PublicRefDefinition("op", "Operation"),
    PublicRefDefinition("call", "ToolInvocation"),
    PublicRefDefinition("trace", "ConversationalToolTrace"),
    PublicRefDefinition("aud", "AuditEvent"),
    PublicRefDefinition("log", "RuntimeLogEntry"),
    PublicRefDefinition("gwy", "GatewayLifetime"),
    PublicRefDefinition("drain", "DrainBarrier"),
    PublicRefDefinition("ing", "DeferredIngress"),
)

PUBLIC_REF_TYPES = MappingProxyType(
    {definition.prefix: definition.canonical_type for definition in _DEFINITIONS}
)
PUBLIC_REF_PREFIXES = frozenset(PUBLIC_REF_TYPES)
PUBLIC_REF_PREFIX_BY_TYPE = MappingProxyType(
    {definition.canonical_type: definition.prefix for definition in _DEFINITIONS}
)

if len(PUBLIC_REF_TYPES) != len(_DEFINITIONS):  # pragma: no cover - import invariant
    raise RuntimeError("Duplicate Docket public-reference prefix")
if len(PUBLIC_REF_PREFIX_BY_TYPE) != len(_DEFINITIONS):  # pragma: no cover
    raise RuntimeError("A Docket public-reference type has multiple prefixes")


def _encode_ulid(value: int) -> str:
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(encoded)


def new_public_ref(prefix: str) -> str:
    """Return a typed, time-sortable public reference using a ULID payload."""
    if prefix not in PUBLIC_REF_PREFIXES:
        raise ValueError(f"Unsupported public-reference prefix: {prefix}")
    timestamp_ms = int(time.time_ns() // 1_000_000)
    value = (timestamp_ms << 80) | secrets.randbits(80)
    return f"{prefix}_{_encode_ulid(value)}"


def new_internal_key() -> str:
    """Return an opaque durable coordination key that is not a public reference."""
    return uuid.uuid4().hex


def parse_public_ref(value: str) -> tuple[str, str]:
    match = _PUBLIC_REF.fullmatch(value)
    if match is None or match.group("prefix") not in PUBLIC_REF_PREFIXES:
        raise ValueError("Invalid Docket public reference")
    return match.group("prefix"), match.group("ulid")


def public_ref_type(value: str) -> str:
    prefix, _ulid = parse_public_ref(value)
    return PUBLIC_REF_TYPES[prefix]


def prefix_for_type(canonical_type: str) -> str:
    try:
        return PUBLIC_REF_PREFIX_BY_TYPE[canonical_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported public-reference type: {canonical_type}") from exc


def is_public_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_public_ref(value)
    except ValueError:
        return False
    return True

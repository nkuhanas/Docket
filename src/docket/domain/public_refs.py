from __future__ import annotations

import re
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PUBLIC_REF = re.compile(r"^(?P<prefix>[a-z][a-z0-9]{1,7})_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})$")

PUBLIC_REF_PREFIXES = frozenset(
    {
        "aff",
        "aud",
        "brief",
        "call",
        "case",
        "chg",
        "cnf",
        "ctx",
        "dec",
        "ent",
        "evt",
        "fact",
        "idn",
        "int",
        "item",
        "lane",
        "log",
        "op",
        "pref",
        "rel",
        "route",
        "rsp",
        "ses",
        "src",
        "stm",
        "tri",
        "turn",
        "utt",
    }
)


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


def parse_public_ref(value: str) -> tuple[str, str]:
    match = _PUBLIC_REF.fullmatch(value)
    if match is None or match.group("prefix") not in PUBLIC_REF_PREFIXES:
        raise ValueError("Invalid Docket public reference")
    return match.group("prefix"), match.group("ulid")


def is_public_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_public_ref(value)
    except ValueError:
        return False
    return True

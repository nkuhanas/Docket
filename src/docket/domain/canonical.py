import hashlib
import json
import re
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_identity(value: str) -> str:
    normalized = _NON_ALNUM.sub("-", value.casefold().strip()).strip("-")
    if not normalized:
        raise ValueError("Canonical identity values must contain letters or digits")
    return normalized


def canonical_record_key(record_type: str, identity: dict[str, Any]) -> str:
    if record_type == "term":
        institution = normalize_identity(str(identity["institution"]))
        term_name = normalize_identity(str(identity["term_name"]))
        return f"term:{institution}:{term_name}"
    if record_type == "course":
        term_record_id = str(identity["term_record_id"])
        course_code = normalize_identity(str(identity["course_code"]))
        section_value = identity.get("section")
        section = normalize_identity(str(section_value)) if section_value else "none"
        return f"course:{term_record_id}:{course_code}:{section}"

    supplied = identity.get("key")
    if not supplied:
        raise ValueError(f"record_type={record_type!r} requires canonical_identity.key")
    return f"{normalize_identity(record_type)}:{normalize_identity(str(supplied))}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()

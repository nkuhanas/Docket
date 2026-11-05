"""Drop redundant schema titles without removing business fields named title."""

from typing import Any

_SCHEMA_MAPS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
_SCHEMA_CHILDREN = frozenset(
    {
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
    }
)
_SCHEMA_LISTS = frozenset({"oneOf", "anyOf", "allOf", "prefixItems"})


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            value = {name: compact_schema(child) for name, child in value.items()}
        elif key in _SCHEMA_CHILDREN and isinstance(value, dict):
            value = compact_schema(value)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            value = [compact_schema(child) for child in value]
        result[key] = value
    return result

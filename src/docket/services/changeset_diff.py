"""Read-only, revision-to-revision draft diffs with lossless bounded detail.

These are changes to staged intent, not a claim that canonical/provider state
has changed. Never consult live objects while paging an immutable draft diff.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from docket.models import ChangeSetRevision

_MISSING = object()
_ACTION_GROUPS = (
    "registry_changes",
    "preference_changes",
    "lane_changes",
    "event_changes",
    "tracked_context_changes",
    "resolution_changes",
)
_ENTRY_INTERNAL = {
    "statement_ref",
    "compiler_identifier",
    "compiler_version",
    "input_schema_version",
    "normalized_input_hash",
    "compilation_errors",
}
_ACTION_INTERNAL = {"basis_refs", "affected_fields"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bounded_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split an oversized detail, never drop it or let its first row evade the budget.

    Fragment offsets are UTF-8 byte offsets in the exact serialized detail. Each
    fragment contains valid Unicode and can be reconstructed without data loss.
    """
    rows: list[dict[str, Any]] = []
    for index, detail in enumerate(details):
        encoded = _json(detail).encode()
        if len(encoded) <= 5_000:
            rows.append(detail)
            continue
        digest = hashlib.sha256(encoded).hexdigest()
        offset = 0
        while offset < len(encoded):
            end = min(offset + 2_000, len(encoded))
            while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
                end -= 1
            rows.append(
                {
                    "detail_index": index,
                    "detail_fragment": {
                        "encoding": "json_utf8",
                        "sha256": digest,
                        "byte_offset": offset,
                        "total_bytes": len(encoded),
                        "text": encoded[offset:end].decode(),
                    },
                }
            )
            offset = end
    return rows


def bounded_sample(details: list[dict[str, Any]], *, budget: int = 2_000) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for detail in details[:5]:
        if len(_json([*result, detail]).encode()) > budget:
            break
        result.append(detail)
    return result


def _fields(before: Any, after: Any, path: list[str | int]) -> Iterator[dict[str, Any]]:
    if before is not _MISSING and after is not _MISSING and type(before) is type(after):
        if isinstance(before, dict):
            for key in sorted(before.keys() | after.keys()):
                yield from _fields(
                    before.get(key, _MISSING), after.get(key, _MISSING), [*path, key]
                )
            return
        if isinstance(before, list):
            for index in range(max(len(before), len(after))):
                yield from _fields(
                    before[index] if index < len(before) else _MISSING,
                    after[index] if index < len(after) else _MISSING,
                    [*path, index],
                )
            return
        if before == after:
            return
    # Flatten added/removed containers so a new entry has useful title/time rows.
    present = after if before is _MISSING else before
    if (before is _MISSING or after is _MISSING) and isinstance(present, dict) and present:
        for key in sorted(present):
            yield from _fields(
                _MISSING if before is _MISSING else present[key],
                _MISSING if after is _MISSING else present[key],
                [*path, key],
            )
        return
    yield {
        "field_path": path,
        "before_present": before is not _MISSING,
        "after_present": after is not _MISSING,
        **({"before": before} if before is not _MISSING else {}),
        **({"after": after} if after is not _MISSING else {}),
    }


def _actions(revision: ChangeSetRevision | None) -> list[dict[str, Any]]:
    if revision is None:
        return []
    if revision.staged_actions_json is not None:
        return revision.staged_actions_json
    return [item for group in _ACTION_GROUPS for item in getattr(revision, group)]


def draft_diff(
    before: ChangeSetRevision | None,
    after: ChangeSetRevision,
    *,
    mutation_types: list[str],
    entry_types: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Compare semantic input values, including removals and scope changes.

    Compiler-owned actions are represented by their owning entry, not repeated
    Item/Time/Event boilerplate. Filters match either side so a type replacement
    or removal never disappears just because its new type differs.
    """
    details: list[dict[str, Any]] = []
    counts = {"added": 0, "removed": 0, "modified": 0}
    old_entries = before.normalized_entries_json if before is not None else []
    new_entries = after.normalized_entries_json
    owned = {
        change_id
        for revision in (before, after)
        if revision is not None
        for owner in revision.compiled_action_ownership_json
        for change_id in owner.get("change_ids", [])
    }
    for kind, old_items, new_items, id_field, type_field, filters, omitted in (
        (
            "entry",
            old_entries,
            new_entries,
            "import_entry_id",
            "entry_type",
            entry_types,
            _ENTRY_INTERNAL,
        ),
        (
            "action",
            _actions(before),
            _actions(after),
            "change_id",
            "mutation_type",
            mutation_types,
            _ACTION_INTERNAL,
        ),
    ):
        old = {str(item[id_field]): item for item in old_items}
        new = {str(item[id_field]): item for item in new_items}
        for identifier in sorted(old.keys() | new.keys()):
            if kind == "action" and identifier in owned:
                continue
            old_item, new_item = old.get(identifier), new.get(identifier)
            if filters and not any(
                item is not None and item.get(type_field) in filters
                for item in (old_item, new_item)
            ):
                continue
            old_value = (
                {key: value for key, value in old_item.items() if key not in omitted}
                if old_item is not None
                else _MISSING
            )
            new_value = (
                {key: value for key, value in new_item.items() if key not in omitted}
                if new_item is not None
                else _MISSING
            )
            changes = list(_fields(old_value, new_value, []))
            if not changes:
                continue
            disposition = (
                "added" if old_item is None else "removed" if new_item is None else "modified"
            )
            counts[disposition] += 1
            header = {"subject_kind": kind, id_field: identifier, "change": disposition}
            details.extend({**header, **change} for change in changes)
    return details, counts

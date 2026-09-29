"""Project current compiler records back to their retained, typed source actions.

Not a decoder for old event snapshots: only current materialized records carry
the source patch that makes this projection possible.
"""

from __future__ import annotations

from typing import Any

from docket.schemas.authority import ChangeSetContent, MaterializedEventModify, mutation_input_json


def event_source_content(content: ChangeSetContent) -> dict[str, Any]:
    raw = mutation_input_json(content, exclude={"provider_intents", "occurrence_plans"})
    owned = {key for edit in content.occurrence_plans for key in edit.action_hashes}
    sources = {edit.source_change_id: edit.source_change for edit in content.occurrence_plans}
    for change in content.event_changes:
        if change.change_id not in owned:
            source = change.source_patch if isinstance(change, MaterializedEventModify) else change
            sources[change.change_id] = mutation_input_json(source)
    raw["event_changes"] = sorted(sources.values(), key=lambda row: row["change_id"])
    raw["lane_changes"] = [row for row in raw["lane_changes"]
                           if (row.get("create_spec") or {}).get("event_change_id") not in owned]
    return raw


def owned_event_action_ids(content: ChangeSetContent, source_id: str) -> set[str]:
    result = {source_id}
    occurrence_owned: set[str] = set()
    for edit in content.occurrence_plans:
        if edit.source_change_id == source_id:
            occurrence_owned.update(edit.action_hashes)
    result.update(occurrence_owned)
    for change in content.lane_changes:
        if change.mutation_type == "lane_routing_decision_create" and (
            change.create_spec.event_change_id in occurrence_owned
        ):
            result.add(change.change_id)
    return result

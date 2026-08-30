from __future__ import annotations

from datetime import date

import pytest

from docket.domain.public_refs import new_public_ref
from docket.models import (
    CanonicalEvent,
    Entity,
    EventItemLink,
    Fact,
    Item,
    Task,
    TemporalBinding,
)
from docket.services.network import NetworkQueryService
from docket.services.tracked_context import TrackedContextService


@pytest.mark.integration
def test_item_context_and_network_preserve_typed_primitive_boundaries(session) -> None:
    changeset_ref = new_public_ref("chg")
    course = Entity(
        entity_kind="course_section",
        display_name="MATH 1263 F26",
        normalized_name="math 1263 f26",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    session.add(course)
    session.flush()
    item = Item(
        title="Midterm Exam",
        kind="academic.exam",
        context_entity_refs=[course.ref_id],
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    session.add(item)
    session.flush()
    fact = Fact(
        subject_ref=item.ref_id,
        predicate="coverage",
        value_json={"chapters": [11, 12]},
        status="active",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    task = Task(
        item_ref=item.ref_id,
        title="Review for the midterm",
        task_state="not_started",
        priority="normal",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    binding = TemporalBinding(
        subject_ref=item.ref_id,
        role="scheduled_on",
        temporal_value={
            "kind": "date",
            "date": date(2026, 9, 18).isoformat(),
            "timezone": "America/Los_Angeles",
        },
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    event = CanonicalEvent(
        canonical_key="math-1263-f26-midterm",
        title="MATH 1263 Midterm",
        status="active",
        event_spec={
            "title": "MATH 1263 Midterm",
            "timing": {
                "kind": "timed",
                "start_local": "2026-09-18T10:10:00",
                "end_local": "2026-09-18T11:00:00",
                "timezone": "America/Los_Angeles",
            },
        },
        authority="explicit_operator",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref=changeset_ref,
    )
    session.add_all([fact, task, binding, event])
    session.flush()
    session.add(
        EventItemLink(
            event_ref=event.ref_id,
            item_ref=item.ref_id,
            realizes_temporal_binding_ref=binding.ref_id,
            basis_refs=[],
        )
    )
    session.flush()

    # Item has canonical-history state, but does not steal Task or Event lifecycle.
    item_columns = set(Item.__table__.c.keys())
    assert "task_state" not in item_columns
    assert "event_status" not in item_columns

    query = TrackedContextService(session).query_items(
        text="Midterm",
        kind="academic.exam",
        context_entity_ref=course.ref_id,
        parent_item_ref=None,
        temporal_role="scheduled_on",
        date_from=date(2026, 9, 18),
        date_to=date(2026, 9, 18),
        has_open_task=True,
        source_ref=None,
        cursor=None,
        limit=25,
    )
    assert query["count"] == 1
    assert query["items"][0]["ref"] == item.ref_id

    context = TrackedContextService(session).item_context(item.ref_id)
    assert context["context_entity_refs"] == [course.ref_id]
    assert context["facts"] == [
        {"ref": fact.ref_id, "predicate": "coverage", "value": {"chapters": [11, 12]}}
    ]
    assert context["tasks"][0]["ref"] == task.ref_id
    assert context["temporal_bindings"][0]["ref"] == binding.ref_id
    assert context["linked_events"][0]["ref"] == event.ref_id

    graph = NetworkQueryService(session).context_neighborhood(
        root_ref=course.ref_id,
        depth=2,
        max_nodes=25,
    )
    nodes = {node["ref"]: node["type"] for node in graph["nodes"]}
    assert nodes == {
        course.ref_id: "course_section",
        item.ref_id: "item",
        task.ref_id: "task",
        binding.ref_id: "temporal_binding",
        event.ref_id: "event",
    }
    predicates = {edge["predicate"] for edge in graph["edges"]}
    assert {"context_for", "has_task", "scheduled_on", "realized_as_event"} <= predicates

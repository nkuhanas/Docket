"""Derived title ownership and a shared Item → Task lock order."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models.base import utc_now
from docket.models.tracked_context import Item, Task, TaskItemTitleOwnership


def lock_title_targets(session: Session, changes: list[Any]) -> None:
    task_refs = sorted(
        {
            change.object_ref
            for change in changes
            if change.object_type == "task" and change.object_ref
        }
    )
    item_refs = {
        change.object_ref
        for change in changes
        if change.object_type == "item" and change.object_ref
    }
    item_refs.update(session.scalars(select(Task.item_ref).where(Task.ref_id.in_(task_refs))))
    for change in changes:
        if change.object_type == "task":
            spec = change.create_spec if change.action == "create" else change.payload
            if spec is not None:
                ref = getattr(spec, "item_ref", None)
                if ref:
                    item_refs.add(ref)
    if item_refs:
        session.scalars(
            select(Item)
            .where(Item.ref_id.in_(sorted(item_refs)))
            .order_by(Item.ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    if task_refs:
        tasks = session.scalars(
            select(Task)
            .where(Task.ref_id.in_(task_refs))
            .order_by(Task.ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        # A concurrent relink before our locks requires a fresh observed version.
        if any(task.item_ref not in item_refs for task in tasks):
            from docket.domain.errors import DocketError

            raise DocketError(
                code="version_conflict", message="The task's Item changed; refresh context."
            )


def relinquish(session: Session, item_ref: str, changeset_ref: str) -> None:
    ownership = session.get(TaskItemTitleOwnership, item_ref)
    if ownership is not None and ownership.relinquished_at is None:
        ownership.relinquished_by_changeset_ref = changeset_ref
        ownership.relinquished_at = utc_now()


def title_relationship(session: Session, item_ref: str) -> dict[str, Any]:
    ownership = session.get(TaskItemTitleOwnership, item_ref)
    if ownership is None:
        return {"mode": "independent"}
    return {
        "mode": "derived" if ownership.relinquished_at is None else "independent",
        "derived_from_task_ref": ownership.task_ref,
        "creation_changeset_ref": ownership.created_by_changeset_ref,
        **(
            {"relinquished_by_changeset_ref": ownership.relinquished_by_changeset_ref}
            if ownership.relinquished_at
            else {}
        ),
    }

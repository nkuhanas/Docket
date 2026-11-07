"""Task graph → lists → Items → Tasks, followed by derived title ownership."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models.base import utc_now
from docket.models.google_tasks import (
    GoogleTaskList,
    GoogleTaskObservation,
    TaskProviderBinding,
    TaskSyncConflict,
)
from docket.models.tracked_context import Item, Task, TaskItemTitleOwnership
from docket.services.notifications import identity_lock
from docket.services.work_attribution import read_operator


def lock_title_targets(session: Session, changes: list[Any]) -> None:
    task_types = {"task", "task_google_link", "task_sync_resolution", "task_list"}
    if any(change.object_type == "task_list" for change in changes):
        identity_lock(session, "task-list-default:" + read_operator())
    if any(change.object_type in task_types for change in changes):
        identity_lock(session, "task-graph:" + read_operator())
    task_refs = sorted(
        {
            change.object_ref
            for change in changes
            if change.object_type in {"task", "task_google_link"} and change.object_ref
        }
    )
    extra_lists: set[str] = set()
    from docket.services.task_subtrees import descendants

    for change in changes:
        if change.object_type == "task" and change.action != "create":
            task_refs.extend(child.ref_id for child in descendants(session, change.object_ref))
    for change in changes:
        if change.object_type == "task_list":
            extra_lists.add(change.object_ref)
        elif change.object_type == "task_google_link":
            ref = getattr(change.payload, "observation_ref", None)
            observation = session.get(GoogleTaskObservation, ref) if ref else None
            if observation is not None:
                extra_lists.add(observation.task_list_ref)
        elif change.object_type == "task_sync_resolution":
            conflict = session.get(TaskSyncConflict, change.object_ref)
            binding = session.get(TaskProviderBinding, conflict.binding_ref) if conflict else None
            if binding:
                task_refs.append(binding.task_ref)
    for change in changes:
        if change.object_type == "task":
            spec = change.create_spec if change.action == "create" else change.payload
            if spec is not None:
                task_refs.extend(
                    ref
                    for ref in (
                        getattr(spec, "parent_task_ref", None),
                        getattr(spec, "after_task_ref", None),
                    )
                    if ref
                )
    item_refs = {
        change.object_ref
        for change in changes
        if change.object_type == "item" and change.object_ref
    }
    for change in changes:
        if change.object_type == "task":
            spec = change.create_spec if change.action == "create" else change.payload
            ref = getattr(spec, "task_list_ref", None)
            if ref:
                extra_lists.add(ref)
            ref = getattr(spec, "item_ref", None)
            if ref:
                item_refs.add(ref)
    lock_task_rows(
        session,
        task_refs,
        lists=sorted(extra_lists),
        items=sorted(item_refs),
        graph=any(change.object_type in task_types for change in changes),
    )


def lock_task_rows(
    session: Session,
    task_refs: Sequence[str] = (),
    *,
    lists: Sequence[str] = (),
    items: Sequence[str] = (),
    graph: bool = True,
) -> None:
    """Shared by canonical commands, imports and delivery callbacks; no authority is granted."""
    if graph:
        identity_lock(session, "task-graph:" + read_operator())
    task_refs = sorted(set(task_refs))
    list_refs = set(session.scalars(select(Task.task_list_ref).where(Task.ref_id.in_(task_refs))))
    list_refs.update(
        session.scalars(
            select(TaskProviderBinding.task_list_ref).where(
                TaskProviderBinding.task_ref.in_(task_refs),
            )
        )
    )
    list_refs.update(lists)
    if any(list_refs):
        session.scalars(
            select(GoogleTaskList)
            .where(GoogleTaskList.ref_id.in_(sorted(ref for ref in list_refs if ref)))
            .order_by(GoogleTaskList.ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    item_refs = set(items)
    item_refs.update(session.scalars(select(Task.item_ref).where(Task.ref_id.in_(task_refs))))
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
    if item_refs:
        session.scalars(
            select(TaskItemTitleOwnership)
            .where(TaskItemTitleOwnership.item_ref.in_(sorted(item_refs)))
            .order_by(TaskItemTitleOwnership.item_ref)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    if task_refs:
        session.scalars(
            select(TaskProviderBinding)
            .where(TaskProviderBinding.task_ref.in_(task_refs))
            .order_by(TaskProviderBinding.ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()


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

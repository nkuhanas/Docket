"""Resolve immutable time components internally; routine commands remain flat."""

from typing import Any

from sqlalchemy import String, case, cast, func, literal, select
from sqlalchemy.orm import Session

from docket.models.work import WorkSessionRevision
from docket.models.work_components import WorkTimeComponent


def time_leaves(roots: Any) -> Any:
    """Expand a bounded source graph in SQL, retaining each report root and stable order."""
    tree = select(
        roots.c.ref_id.label("root_ref"),
        roots.c.ref_id.label("source_ref"),
        literal("").label("path"),
    ).cte(recursive=True, nesting=True)
    component = WorkTimeComponent
    ordinal = case(
        (component.ordinal < 10, literal("0") + cast(component.ordinal, String)),
        else_=cast(component.ordinal, String),
    )
    tree = tree.union_all(
        select(
            tree.c.root_ref,
            component.source_revision_ref,
            (tree.c.path + literal(".") + ordinal).label("path"),
        ).join(component, component.revision_ref == tree.c.source_ref)
    )
    source = WorkSessionRevision.__table__.alias()
    return (
        select(
            tree.c.root_ref,
            tree.c.path,
            *source.c,
        )
        .join(source, source.c.ref_id == tree.c.source_ref)
        .where(
            source.c.time_precision != "composite",
        )
        .subquery()
    )


def component_leaves(
    session: Session, revision_refs: list[str], *, limit: int | None = None
) -> list[WorkSessionRevision]:
    if not revision_refs:
        return []
    roots = (
        select(WorkSessionRevision.ref_id)
        .where(
            WorkSessionRevision.ref_id.in_(revision_refs),
        )
        .subquery()
    )
    leaves = time_leaves(roots)
    order = case({ref: index for index, ref in enumerate(revision_refs)}, value=leaves.c.root_ref)
    statement = (
        select(WorkSessionRevision)
        .join(
            leaves,
            leaves.c.ref_id == WorkSessionRevision.ref_id,
        )
        .order_by(order, leaves.c.path)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def component_quantity(session: Session, revision_refs: list[str]) -> tuple[int, bool]:
    if not revision_refs:
        return 0, False
    roots = (
        select(WorkSessionRevision.ref_id)
        .where(
            WorkSessionRevision.ref_id.in_(revision_refs),
        )
        .subquery()
    )
    leaves = time_leaves(roots)
    row = session.execute(
        select(
            func.coalesce(func.sum(leaves.c.duration_us), 0),
            func.coalesce(func.sum(case((leaves.c.duration_us.is_(None), 1), else_=0)), 0),
        )
    ).one()
    return int(row[0]), bool(row[1])

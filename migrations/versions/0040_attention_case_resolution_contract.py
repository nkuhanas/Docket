"""Type AttentionCase revisions and preserve honest CaseItem resolution roles.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _new_ref(prefix: str) -> str:
    value = (int(time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return prefix + "_" + "".join(encoded)


def _rewrite_scalar_refs(
    bind: Connection,
    table_name: str,
    column_name: str,
    mapping: Mapping[str, str],
) -> None:
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    column = table.c[column_name]
    for old_ref, new_ref in mapping.items():
        bind.execute(table.update().where(column == old_ref).values({column_name: new_ref}))


def _rewrite_json_list_refs(
    bind: Connection,
    table_name: str,
    column_name: str,
    mapping: Mapping[str, str],
) -> None:
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    column = table.c[column_name]
    primary_keys = list(table.primary_key.columns)
    if len(primary_keys) != 1:
        raise RuntimeError(f"{table_name} must have one primary key for revision migration")
    primary_key = primary_keys[0]
    for row in bind.execute(sa.select(primary_key, column)):
        values = list(row[1] or [])
        rewritten = [mapping.get(value, value) for value in values]
        if rewritten != values:
            bind.execute(
                table.update()
                .where(primary_key == row[0])
                .values({column_name: rewritten})
            )


def _rewrite_revision_bindings(
    bind: Connection,
    mapping: Mapping[str, str],
) -> None:
    for table_name, column_name in (
        ("daily_brief_case_items", "case_revision_ref"),
        ("queue_items", "attention_case_revision_ref"),
        ("discord_projections", "primary_revision_ref"),
    ):
        _rewrite_scalar_refs(bind, table_name, column_name, mapping)
    for table_name, column_name in (
        ("intent_sessions", "case_revision_refs"),
        ("agent_response_projections", "case_revision_refs"),
    ):
        _rewrite_json_list_refs(bind, table_name, column_name, mapping)


def _revision_rows(bind: Connection) -> list[tuple[Any, str, str | None]]:
    revisions = sa.Table(
        "attention_case_revisions", sa.MetaData(), autoload_with=bind
    )
    return [
        (row.id, row.ref_id, row.legacy_ref_id)
        for row in bind.execute(
            sa.select(revisions.c.id, revisions.c.ref_id, revisions.c.legacy_ref_id)
        )
    ]


def upgrade() -> None:
    with op.batch_alter_table("attention_case_revisions") as batch:
        batch.add_column(sa.Column("legacy_ref_id", sa.String(length=40)))
        batch.create_unique_constraint(
            "uq_attention_case_revisions_legacy_ref_id", ["legacy_ref_id"]
        )

    with op.batch_alter_table("case_items") as batch:
        batch.add_column(
            sa.Column(
                "resolution_role",
                sa.String(length=32),
                server_default="legacy_unspecified",
                nullable=False,
            )
        )
        batch.drop_constraint("ck_case_items_status", type_="check")
        batch.create_check_constraint(
            "ck_case_items_status",
            "status IN ('open', 'resolved', 'rejected', 'not_pursued')",
        )
        batch.create_check_constraint(
            "ck_case_items_resolution_role",
            "resolution_role IN ('required', 'supporting', 'legacy_unspecified')",
        )

    bind = op.get_bind()
    revisions = sa.Table(
        "attention_case_revisions", sa.MetaData(), autoload_with=bind
    )
    mapping: dict[str, str] = {}
    for revision_id, old_ref, _legacy_ref in _revision_rows(bind):
        new_ref = _new_ref("caserev")
        mapping[old_ref] = new_ref
        bind.execute(
            revisions.update()
            .where(revisions.c.id == revision_id)
            .values(ref_id=new_ref, legacy_ref_id=old_ref)
        )
    _rewrite_revision_bindings(bind, mapping)

    with op.batch_alter_table("case_items") as batch:
        batch.alter_column(
            "resolution_role",
            existing_type=sa.String(length=32),
            server_default=None,
        )


def downgrade() -> None:
    bind = op.get_bind()
    revisions = sa.Table(
        "attention_case_revisions", sa.MetaData(), autoload_with=bind
    )
    mapping: dict[str, str] = {}
    for revision_id, current_ref, legacy_ref in _revision_rows(bind):
        restored_ref = legacy_ref or _new_ref("case")
        mapping[current_ref] = restored_ref
        bind.execute(
            revisions.update()
            .where(revisions.c.id == revision_id)
            .values(ref_id=restored_ref)
        )
    _rewrite_revision_bindings(bind, mapping)

    case_items = sa.Table("case_items", sa.MetaData(), autoload_with=bind)
    bind.execute(
        case_items.update()
        .where(case_items.c.status == "not_pursued")
        .values(status="open")
    )
    with op.batch_alter_table("case_items") as batch:
        batch.drop_constraint("ck_case_items_resolution_role", type_="check")
        batch.drop_constraint("ck_case_items_status", type_="check")
        batch.create_check_constraint(
            "ck_case_items_status",
            "status IN ('open', 'resolved', 'rejected')",
        )
        batch.drop_column("resolution_role")
    with op.batch_alter_table("attention_case_revisions") as batch:
        batch.drop_constraint(
            "uq_attention_case_revisions_legacy_ref_id", type_="unique"
        )
        batch.drop_column("legacy_ref_id")

"""add item source semantic key

Revision ID: 20260905f4c1
Revises: 2022877699cf
Create Date: 2026-09-05 19:30:00.000000
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260905f4c1"
down_revision: str | None = "2022877699cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_immutable_trigger(*, enabled: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    disposition = "ENABLE" if enabled else "DISABLE"
    op.execute(
        f"ALTER TABLE item_source_bindings {disposition} TRIGGER trg_item_source_bindings_immutable"
    )


def _semantic_key(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _backfill_semantic_keys() -> None:
    bind = op.get_bind()
    bindings = sa.table(
        "item_source_bindings",
        sa.column("id", sa.Uuid()),
        sa.column("item_ref", sa.String()),
        sa.column("locator_hash", sa.String()),
        sa.column("semantic_role", sa.String()),
        sa.column("basis_refs", sa.JSON()),
        sa.column("semantic_key", sa.String()),
    )
    statements = sa.table(
        "interpreted_statements",
        sa.column("ref_id", sa.String()),
        sa.column("predicate", sa.String()),
        sa.column("value_json", sa.JSON()),
        sa.column("affected_fields", sa.JSON()),
    )
    statement_rows = bind.execute(
        sa.select(
            statements.c.ref_id,
            statements.c.predicate,
            statements.c.value_json,
            statements.c.affected_fields,
        )
    ).mappings()
    statement_by_ref = {row["ref_id"]: row for row in statement_rows}
    binding_rows = list(
        bind.execute(
            sa.select(
                bindings.c.id,
                bindings.c.item_ref,
                bindings.c.locator_hash,
                bindings.c.semantic_role,
                bindings.c.basis_refs,
            )
        ).mappings()
    )
    for row in binding_rows:
        candidates = [
            statement_by_ref[ref]
            for ref in row["basis_refs"] or []
            if ref in statement_by_ref
            and statement_by_ref[ref]["predicate"] == row["semantic_role"]
        ]
        if len(candidates) == 1:
            statement = candidates[0]
            semantic_key = _semantic_key(
                {
                    "predicate": statement["predicate"],
                    "value": statement["value_json"],
                    "affected_fields": statement["affected_fields"],
                }
            )
        else:
            semantic_key = _semantic_key(
                {
                    "legacy_locator_hash": row["locator_hash"],
                    "semantic_role": row["semantic_role"],
                    "item_ref": row["item_ref"],
                }
            )
        bind.execute(
            sa.update(bindings).where(bindings.c.id == row["id"]).values(semantic_key=semantic_key)
        )


def upgrade() -> None:
    op.add_column(
        "item_source_bindings",
        sa.Column("semantic_key", sa.String(length=64), nullable=True),
    )
    _set_immutable_trigger(enabled=False)
    _backfill_semantic_keys()
    _set_immutable_trigger(enabled=True)
    with op.batch_alter_table("item_source_bindings") as batch_op:
        batch_op.alter_column(
            "semantic_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.drop_constraint(
            "uq_item_source_bindings_fragment_role",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_item_source_bindings_fragment_semantic_key",
            ["source_ref", "source_revision_key", "locator_hash", "semantic_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("item_source_bindings") as batch_op:
        batch_op.drop_constraint(
            "uq_item_source_bindings_fragment_semantic_key",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_item_source_bindings_fragment_role",
            ["source_ref", "source_revision_key", "locator_hash", "semantic_role"],
        )
        batch_op.drop_column("semantic_key")

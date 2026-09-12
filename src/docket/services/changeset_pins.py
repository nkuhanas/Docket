"""Pin stored executable effects without decoding previous input schemas.

Compiler versions describe how a revision was produced, not what must be
installed to execute it. Execution accepts only the current canonical schema
and verifies that parsing it did not change the immutable stored effects.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import ChangeSet, ChangeSetRevision
from docket.schemas.common import StrictModel

COMPILER_IDENTIFIER = "docket.changeset"
COMPILER_VERSION = 1
EXECUTABLE_SCHEMA_VERSION = 1
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EntryCompilerPin(StrictModel):
    entry_id: str
    identifier: str
    version: int = Field(ge=1)
    input_schema_version: int = Field(ge=1)


class DraftExecutionPin(StrictModel):
    format_version: Literal[1] = 1
    executable_schema_version: int = Field(ge=1)
    compiler_identifier: str
    compiler_version: int = Field(ge=1)
    normalized_entry_compilers: list[EntryCompilerPin]
    input_hash: Digest
    compiled_effect_hash: Digest | None
    execution_precondition_hash: Digest
    binding_hash: Digest


def effect_hash(payload: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in payload.items() if key != "expected_versions"})


def _input_hash(snapshot: ChangeSet | ChangeSetRevision) -> str:
    return sha256_json(
        {
            "actions": snapshot.staged_actions_json,
            "entries": snapshot.normalized_entries_json,
            "ownership": snapshot.compiled_action_ownership_json,
        }
    )


def _binding_hash(snapshot: ChangeSet | ChangeSetRevision) -> str:
    return sha256_json(
        {
            "request": snapshot.semantic_request_ref,
            "authority": snapshot.authority_scope_hash,
            "precondition": snapshot.precondition_hash,
            "execution_binding": snapshot.execution_binding_json,
        }
    )


def pin_snapshot(changeset: ChangeSet, payload: dict[str, Any] | None) -> None:
    pin = DraftExecutionPin(
        executable_schema_version=EXECUTABLE_SCHEMA_VERSION,
        compiler_identifier=COMPILER_IDENTIFIER,
        compiler_version=COMPILER_VERSION,
        normalized_entry_compilers=[
            EntryCompilerPin(
                entry_id=entry["import_entry_id"],
                identifier=entry["compiler_identifier"],
                version=entry["compiler_version"],
                input_schema_version=entry["input_schema_version"],
            )
            for entry in changeset.normalized_entries_json
        ],
        input_hash=_input_hash(changeset),
        compiled_effect_hash=effect_hash(payload) if payload is not None else None,
        execution_precondition_hash=sha256_json(changeset.expected_versions),
        binding_hash=_binding_hash(changeset),
    )
    changeset.compiler_manifest_json = {
        **(changeset.compiler_manifest_json or {}),
        "execution_pin": pin.model_dump(mode="json"),
    }


def migration_required() -> DocketError:
    return DocketError(
        code="draft_migration_required",
        message="This draft needs an explicit audited migration before it can execute.",
        details={
            "category": "implementation_validation",
            "constraint": "supported_pinned_executable_schema",
            "field_path": ["compiler_manifest", "execution_pin"],
            "authority_preserved": True,
            "next_action": "migrate_draft",
        },
    )


def verify_snapshot(
    changeset: ChangeSet, revision: ChangeSetRevision | None, payload: dict[str, Any] | None,
    *, for_migration: bool = False,
) -> DraftExecutionPin:
    if revision is None:
        raise migration_required()
    try:
        pin = DraftExecutionPin.model_validate(revision.compiler_manifest_json.get("execution_pin"))
    except ValidationError as exc:
        raise migration_required() from exc
    if pin.executable_schema_version != EXECUTABLE_SCHEMA_VERSION and not for_migration:
        raise migration_required()
    if (
        changeset.compiler_manifest_json != revision.compiler_manifest_json
        or pin.input_hash != _input_hash(changeset)
        or pin.input_hash != _input_hash(revision)
        or pin.binding_hash != _binding_hash(changeset)
        or pin.binding_hash != _binding_hash(revision)
        or pin.execution_precondition_hash != sha256_json(changeset.expected_versions)
        or pin.execution_precondition_hash != sha256_json(revision.expected_versions)
        or (payload is not None and pin.compiled_effect_hash != effect_hash(payload))
        or (payload is not None and revision.parameter_hash != sha256_json(payload))
    ):
        raise DocketError(
            code="draft_execution_pin_mismatch",
            message="The draft no longer matches its immutable executable revision.",
            details={
                "revision": revision.revision,
                "category": "implementation_validation",
                "constraint": "immutable_executable_effects",
                "field_path": ["compiler_manifest", "execution_pin"],
                "authority_preserved": True,
                "next_action": "reconcile_draft_revision",
            },
        )
    return pin

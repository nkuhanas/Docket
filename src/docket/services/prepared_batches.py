"""Compose exact owned previews into one atomic canonical/provider transaction."""

import hashlib
from typing import Any

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.models import AuthenticatedRequest, ChangeSet, PreparedCommand, PreparedConsumption
from docket.schemas.authority import CanonicalChangeInput, mutation_input_json
from docket.services.agent_requests import AgentRequestService
from docket.services.business_commands import context_targets, mutation_plan
from docket.services.native_commands import NativeCommandService
from docket.services.native_context import NativeContextService


def _rename(model: BaseModel, names: dict[str, str]) -> BaseModel:
    updates: dict[str, Any] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        if name == "change_id" or name.endswith("_change_id"):
            if isinstance(value, str):
                updates[name] = names.get(value, value)
        elif name.endswith("_change_ids") and isinstance(value, list):
            updates[name] = [names.get(item, item) for item in value]
        elif isinstance(value, BaseModel):
            updates[name] = _rename(value, names)
        elif isinstance(value, list) and any(isinstance(item, BaseModel) for item in value):
            updates[name] = [
                _rename(item, names) if isinstance(item, BaseModel) else item for item in value
            ]
        elif isinstance(value, dict) and any(
            isinstance(item, BaseModel) for item in value.values()
        ):
            updates[name] = {
                key: _rename(item, names) if isinstance(item, BaseModel) else item
                for key, item in value.items()
            }
    return model.model_copy(update=updates)


def members(service: NativeCommandService, proposal: PreparedCommand) -> list[PreparedCommand]:
    principal = require_principal("commit")
    refs = proposal.preview.get("member_change_ids", [])
    rows = list(
        service.session.scalars(
            select(PreparedCommand).where(
                PreparedCommand.ref_id.in_(refs),
                PreparedCommand.principal_ref == principal.principal_ref,
            )
        )
    )
    if len(rows) != len(refs):
        raise DocketError(
            code="batch_member_unavailable", message="Read the owned member previews."
        )
    # The caller holds the owner grant and batch request lock. Member locks remain
    # held through canonical commit and consumption, including concurrent retries.
    for row in sorted(rows, key=lambda item: item.request_ref):
        root = AgentRequestService(service.session).require(
            row.request_ref, permission="commit", allow_committed=True
        )
        consumed = service.session.get(PreparedConsumption, row.ref_id)
        if consumed is not None and consumed.batch_ref != proposal.ref_id:
            raise DocketError(
                code="batch_member_already_applied",
                message="This preview was applied by another batch.",
            )
        original = service.session.scalar(
            select(ChangeSet)
            .where(
                ChangeSet.ref_id == row.changeset_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            root.state == "committed"
            or original is None
            or original.state == "committed"
            or service.fingerprint(original) != row.revision_hash
        ):
            raise DocketError(
                code="batch_member_changed",
                message="A member changed or committed; prepare a current batch.",
            )
    return rows


def consume(
    service: NativeCommandService, proposal: PreparedCommand, rows: list[PreparedCommand]
) -> None:
    for row in rows:
        service.session.add(PreparedConsumption(member_ref=row.ref_id, batch_ref=proposal.ref_id))
        root = service.session.scalar(
            select(AuthenticatedRequest).where(
                AuthenticatedRequest.ref_id == row.request_ref,
            )
        )
        assert root is not None
        root.state = "committed"
        # Only the batch request owns its ChangeSet. The immutable consumption
        # receipt links each member request to that commit without changing the
        # historical one-request/one-ChangeSet ownership constraint.
    service.session.flush()


class PreparedBatchService(NativeCommandService):
    def prepare(
        self, operation_id: str, change_ids: list[str], request_ref: str, request_key: str
    ) -> dict[str, Any]:
        principal = require_principal("commit")
        AgentRequestService(self.session).require(
            request_ref, permission="commit", allow_committed=True
        )
        if len(set(change_ids)) != len(change_ids):
            raise DocketError(code="duplicate_batch_members", message="List each change_id once.")
        existing = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.principal_ref == principal.principal_ref,
                PreparedCommand.operation_id == operation_id,
            )
        )
        if existing:
            if (
                existing.command != "batch"
                or existing.preview.get("member_change_ids") != change_ids
            ):
                raise DocketError(
                    code="operation_id_reused",
                    message="Reuse operation_id only for unchanged batch retries.",
                )
            return self.prepared(existing)
        selected = list(
            self.session.scalars(
                select(PreparedCommand).where(
                    PreparedCommand.ref_id.in_(change_ids),
                    PreparedCommand.principal_ref == principal.principal_ref,
                )
            )
        )
        by_ref = {row.ref_id: row for row in selected}
        if len(by_ref) != len(change_ids):
            raise DocketError(
                code="change_unavailable", message="Choose your owned prepared changes."
            )
        versions: dict[str, Any] = {}
        targets: set[str] = set()
        actions = []
        touched: set[str] = set()
        adapter: TypeAdapter[CanonicalChangeInput] = TypeAdapter(CanonicalChangeInput)
        for index, ref in enumerate(change_ids):
            row = by_ref[ref]
            AgentRequestService(self.session).require(row.request_ref, permission="commit")
            draft = self.session.scalar(
                select(ChangeSet)
                .where(
                    ChangeSet.ref_id == row.changeset_ref,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if draft is None or self.fingerprint(draft) != row.revision_hash:
                raise DocketError(
                    code="prepared_change_stale", message="Read current exact previews."
                )
            if draft.validation_errors or draft.normalized_entries_json or row.command == "batch":
                raise DocketError(
                    code="batch_member_needs_review",
                    message="Use current ready business previews as batch members.",
                )
            source = NativeContextService(self.session, self.settings).require(row.context_ref)
            targets.update(context_targets(source))
            for target, version in draft.expected_versions.items():
                if target in versions and versions[target] != version:
                    raise DocketError(
                        code="batch_version_conflict",
                        message="Read current context for overlapping targets.",
                    )
                versions[target] = version
            original = draft.staged_actions_json or []
            names = {
                action["change_id"]: f"m{index}."
                + hashlib.sha256(action["change_id"].encode()).hexdigest()[:24]
                for action in original
            }
            for action in original:
                action_target = action.get("object_ref")
                if action_target and action_target in touched:
                    raise DocketError(
                        code="overlapping_batch_changes",
                        message="Combine edits for the same target into one preview.",
                        details={"field": "change_ids", "ref": action_target},
                    )
                if action_target:
                    touched.add(action_target)
                model = _rename(adapter.validate_python(action), names)
                model = model.model_copy(
                    update={
                        "basis_refs": sorted(
                            set(
                                [
                                    *getattr(model, "basis_refs", []),
                                    row.request_ref,
                                ]
                            )
                        )
                    }
                )
                actions.append(mutation_input_json(model, preserve_explicit_nulls=True))
        packet = NativeContextService(self.session, self.settings).retain(
            "batch",
            {
                "objects": [{"ref": ref} for ref in sorted(targets)],
                "coverage": "Exact member references/versions; read members for details.",
            },
            versions,
        )
        arguments = {
            "context_id": packet["context_id"],
            "operation_id": operation_id,
            "member_change_ids": change_ids,
            "preview": True,
        }
        return self.execute(
            command="batch",
            arguments=arguments,
            request_ref=request_ref,
            request_key=request_key,
            build=lambda context: mutation_plan(context, actions, "batch"),
        )

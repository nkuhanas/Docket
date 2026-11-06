"""Compile business commands through the existing exact-revision ChangeSet engine."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    ChangeSet,
    NativeContext,
    NativeOutcome,
    PreparedCommand,
    PreparedConsumption,
)
from docket.schemas.assembly import StageChangesInput
from docket.schemas.calendar import TimedEventTiming
from docket.schemas.native_commands import CreateEvent
from docket.services.agent_requests import AgentRequestService
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.native_context import NativeContextService


def event_timing(start: str, end: str, timezone: str) -> dict[str, Any]:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DocketError(
            code="invalid_timezone",
            message="Use an IANA timezone name.",
            details={"field": "timezone"},
        ) from exc
    values = []
    for name, value in (("start", start), ("end", end)):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DocketError(
                code="invalid_timestamp", message="Use an ISO timestamp.", details={"field": name}
            ) from exc
        if parsed.tzinfo is None:
            candidates = [parsed.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
            valid = [
                candidate
                for candidate in candidates
                if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == parsed
            ]
            if not valid or len({v.utcoffset() for v in valid}) > 1:
                raise DocketError(
                    code="ambiguous_or_nonexistent_time",
                    message="Supply a real local time with its UTC offset.",
                    details={"field": name},
                )
            parsed = valid[0]
        local = parsed.astimezone(zone)
        if local.replace(tzinfo=None) != parsed.replace(tzinfo=None) or (
            local.utcoffset() != parsed.utcoffset()
        ):
            raise DocketError(
                code="timezone_offset_conflict",
                message="Timestamp offset must match timezone at that local time.",
                details={"field": name, "timezone": timezone},
            )
        values.append(local)
    if values[1].astimezone(UTC) <= values[0].astimezone(UTC):
        raise DocketError(
            code="invalid_event_end", message="end must be after start.", details={"field": "end"}
        )
    shared_fold = next(
        (
            fold
            for fold in (0, 1)
            if all(
                value.replace(tzinfo=None).replace(tzinfo=zone, fold=fold).utcoffset()
                == value.utcoffset()
                for value in values
            )
        ),
        None,
    )
    if shared_fold is None:
        raise DocketError(
            code="unsupported_fold_interval",
            message="This interval crosses a repeated clock hour; choose exact times.",
            details={"field": "end"},
        )
    timing = {
        "kind": "timed",
        "start_local": values[0].replace(tzinfo=None).isoformat(),
        "end_local": values[1].replace(tzinfo=None).isoformat(),
        "timezone": timezone,
        "fold": shared_fold,
    }
    try:
        TimedEventTiming.model_validate(timing)
    except ValidationError as exc:
        raise DocketError(
            code="invalid_event_timing",
            message="These bounds cannot represent this local clock interval.",
            details={
                "field": "end",
                "issues": [
                    issue["msg"] for issue in exc.errors(include_input=False, include_url=False)
                ],
            },
        ) from exc
    return timing


class NativeCommandService:
    EXECUTION = "native-exact-proposal"

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _admission(
        self, request_ref: str, operation: str, tool: str, argument_hash: str
    ) -> dict[str, Any]:
        return ChangeSetAssemblyAdmissionService(self.session).admit_agent(
            request_ref=request_ref,
            execution_key=self.EXECUTION,
            operation_key=operation,
            tool_name=tool,
            argument_hash=argument_hash,
        )

    @staticmethod
    def fingerprint(changeset: ChangeSet) -> str:
        return sha256_json(
            {
                "ref": changeset.ref_id,
                "revision": changeset.current_revision,
                "scope": changeset.authority_scope_hash,
                "precondition": changeset.precondition_hash,
                "actions": changeset.staged_actions_json,
                "entries": changeset.normalized_entries_json,
            }
        )

    def require_replacement(self, change_id: str | None, request_ref: str) -> None:
        if change_id is None:
            return
        principal = require_principal("commit")
        proposal = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.ref_id == change_id,
                PreparedCommand.principal_ref == principal.principal_ref,
                PreparedCommand.request_ref == request_ref,
            )
        )
        current = (
            self.session.scalar(
                select(ChangeSet)
                .where(
                    ChangeSet.ref_id == proposal.changeset_ref,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if proposal
            else None
        )
        if proposal is None or current is None:
            raise DocketError(code="change_unavailable", message="Correct your own exact proposal.")
        if self.fingerprint(current) != proposal.revision_hash:
            raise DocketError(
                code="prepared_change_stale",
                message="Another correction changed this proposal; read the retained work.",
            )

    def create_event(
        self, command: CreateEvent, request_ref: str, request_key: str, *, prepare: bool = False
    ) -> dict[str, Any]:
        principal = require_principal("commit")
        AgentRequestService(self.session).require(
            request_ref, permission="commit", allow_committed=True
        )
        self.require_command_identity(request_ref)
        # Preserve retry identity for the first deployed flat contract's default fields.
        original_fields = {
            "context_id",
            "operation_id",
            "title",
            "start",
            "end",
            "timezone",
            "calendar_ref",
            "location",
            "notes",
        }
        hash_input = command.model_dump(mode="json", include=original_fields)
        for key, value in command.model_dump(mode="json").items():
            if key not in original_fields | {"preview", "replaces_change_id"} and value:
                hash_input[key] = value
        if command.replaces_change_id is not None:
            hash_input["replaces_change_id"] = command.replaces_change_id
        argument_hash = sha256_json(hash_input)
        existing = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.principal_ref == principal.principal_ref,
                PreparedCommand.operation_id == command.operation_id,
            )
        )
        if existing:
            if existing.argument_hash != argument_hash or existing.command != "create_event":
                raise DocketError(
                    code="operation_id_reused",
                    message="Reuse operation_id only for unchanged retries; "
                    "use a new ID for changed intent.",
                    details={"field": "operation_id"},
                )
            return self.prepared(existing) if prepare else self.apply(existing.ref_id)
        self.require_replacement(command.replaces_change_id, request_ref)
        context = NativeContextService(self.session, self.settings).require(
            command.context_id,
        )
        if context.purpose not in {"calendar", "objects"}:
            raise DocketError(
                code="context_unavailable", message="Read the calendar or object context."
            )
        from docket.services.event_commands import create_event_plan

        plan = create_event_plan(self.session, context, command, request_key)
        data = StageChangesInput.model_validate(
            {"request_ref": request_ref, "request_key": request_key, **plan}
        )
        if command.replaces_change_id is not None:
            data.assembly_scope = None
        return self.stage_plan(
            command="create_event",
            operation_id=command.operation_id,
            argument_hash=argument_hash,
            context=context,
            data=data,
            preview={
                "title": command.title,
                "start": command.start,
                "end": command.end,
                "timezone": command.timezone,
                "calendar_ref": command.calendar_ref,
                "location": command.location,
                "notes": command.notes,
                **(
                    {"replaces_change_id": command.replaces_change_id}
                    if command.replaces_change_id
                    else {}
                ),
            },
            prepare=prepare,
        )

    def stage_plan(
        self,
        *,
        command: str,
        operation_id: str,
        argument_hash: str,
        context: NativeContext,
        data: StageChangesInput,
        preview: dict[str, Any],
        prepare: bool = False,
    ) -> dict[str, Any]:
        admission = self._admission(
            data.request_ref or "",
            "prepare:" + argument_hash,
            "docket_stage_changes",
            argument_hash,
        )
        staged = ChangeSetAssemblyService(self.session).stage(
            data,
            assembly_operation_token=admission["assembly_operation_token"],
            assembly_argument_hash=argument_hash,
        )
        changeset = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.ref_id == staged["draft_ref"],
            )
        )
        assert changeset is not None
        proposal = PreparedCommand(
            principal_ref=require_principal("commit").principal_ref,
            operation_id=operation_id,
            command=command,
            argument_hash=argument_hash,
            context_ref=context.ref_id,
            request_ref=data.request_ref or "",
            changeset_ref=changeset.ref_id,
            revision=changeset.current_revision,
            revision_hash=self.fingerprint(changeset),
            preview=preview,
        )
        self.session.add(proposal)
        self.session.flush()
        if staged.get("disposition") != "ready_to_commit":
            issues = self.public_issues(changeset.validation_errors)
            return {
                "ok": False,
                "change_id": proposal.ref_id,
                "error": {
                    "code": "command_needs_correction",
                    "message": "Correct the affected fields or follow provider delivery.",
                    "details": {"issues": issues, "next_action": "docket_read_change"},
                },
            }
        return self.prepared(proposal) if prepare else self.apply(proposal.ref_id)

    def execute(
        self,
        *,
        command: str,
        arguments: dict[str, Any],
        request_ref: str,
        request_key: str,
        build: Callable[[NativeContext], dict[str, Any]],
    ) -> dict[str, Any]:
        principal = require_principal("commit")
        AgentRequestService(self.session).require(
            request_ref, permission="commit", allow_committed=True
        )
        self.require_command_identity(request_ref)
        operation_id = str(arguments["operation_id"])
        argument_hash = sha256_json(
            {key: value for key, value in arguments.items() if key != "preview"}
        )
        existing = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.principal_ref == principal.principal_ref,
                PreparedCommand.operation_id == operation_id,
            )
        )
        if existing:
            if existing.argument_hash != argument_hash or existing.command != command:
                raise DocketError(
                    code="operation_id_reused",
                    message="Use a new operation_id for changed intent.",
                    details={"field": "operation_id"},
                )
            return (
                self.prepared(existing) if arguments.get("preview") else self.apply(existing.ref_id)
            )
        self.require_replacement(arguments.get("replaces_change_id"), request_ref)
        context = NativeContextService(self.session, self.settings).require(arguments["context_id"])
        plan = build(context)
        command_preview = plan.pop("command_preview", {})
        if arguments.get("replaces_change_id"):
            plan["assembly_scope"] = None
        request = StageChangesInput.model_validate(
            {"request_ref": request_ref, "request_key": request_key, **plan}
        )
        return self.stage_plan(
            command=command,
            operation_id=operation_id,
            argument_hash=argument_hash,
            context=context,
            data=request,
            preview={
                **{k: v for k, v in arguments.items()
                   if k not in {"operation_id", "context_id", "preview"}},
                **command_preview,
            },
            prepare=bool(arguments.get("preview")),
        )

    @staticmethod
    def public_issues(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues = []
        for error in errors[:20]:
            details = error.get("details") or {}
            code = error.get("code", "invalid_fields")
            path = details.get("field_path", [])
            field = ".".join(
                str(part) for part in path if part not in {"payload", "create_spec", "event_spec"}
            )
            field = {"object_ref": "target_ref", "timing": "start/end/timezone"}.get(field, field)
            issue = {
                "code": code,
                "field": field or "context_id",
                "message": error.get("message", code.replace("_", " ")),
            }
            if "remaining_required_case_item_refs" in details:
                issue["remaining_required_case_item_refs"] = details[
                    "remaining_required_case_item_refs"
                ]
            if code == "provider_event_binding_required":
                issue.update(
                    message="The existing event is awaiting Google delivery or reconciliation; "
                    "follow its original receipt instead of creating another event.",
                    next_action="docket_get_history_entry",
                    delivery=details.get("status_read", {}).get("arguments", {}),
                )
            elif "version" in code or "stale" in code:
                issue.update(
                    message="The selected target changed. Read current context for that target.",
                    next_action="docket_get_context",
                )
            issues.append(issue)
        return issues

    def require_command_identity(self, request_ref: str) -> None:
        if self.session.get(NativeOutcome, request_ref) is not None:
            raise DocketError(code="operation_id_reused", message="Choose a new operation_id.")

    @staticmethod
    def bounded_preview(value: dict[str, Any]) -> dict[str, Any]:
        import json

        result: dict[str, Any] = {}
        omitted = []
        used = 0
        for key, item in value.items():
            size = len(json.dumps(item, ensure_ascii=False).encode())
            if size > 1500 or used + size > 6000:
                omitted.append(key)
                if isinstance(item, str):
                    result[key + "_excerpt"] = item[:200]
            else:
                result[key] = item
                used += size
        if omitted:
            result["omitted_fields"] = omitted
        return result

    def prepared(self, proposal: PreparedCommand) -> dict[str, Any]:
        if self.session.get(PreparedConsumption, proposal.ref_id) is not None:
            return self.apply(proposal.ref_id)
        changeset = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.ref_id == proposal.changeset_ref,
            )
        )
        if changeset is None or self.fingerprint(changeset) != proposal.revision_hash:
            raise DocketError(
                code="prepared_change_stale",
                message="This retained proposal changed; read its exact saved preview.",
            )
        if changeset is not None and changeset.state == "committed":
            return {**changeset.commit_receipt_json, "change_id": proposal.ref_id, "replayed": True}
        return {
            "ok": True,
            "disposition": "prepared",
            "change_id": proposal.ref_id,
            "preview": self.bounded_preview(proposal.preview),
            "next": "docket_apply_change",
        }

    def read(self, change_id: str) -> dict[str, Any]:
        principal = require_principal("read")
        proposal = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.ref_id == change_id,
                PreparedCommand.principal_ref == principal.principal_ref,
            )
        )
        if proposal is None:
            raise DocketError(code="change_unavailable", message="Read your own prepared change.")
        changeset = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.ref_id == proposal.changeset_ref,
            )
        )
        from docket.models import ChangeSetRevision

        revision = (
            self.session.scalar(
                select(ChangeSetRevision).where(
                    ChangeSetRevision.change_set_id == changeset.id,
                    ChangeSetRevision.revision == proposal.revision,
                )
            )
            if changeset is not None
            else None
        )
        stale = changeset is None or self.fingerprint(changeset) != proposal.revision_hash
        consumed = self.session.get(PreparedConsumption, proposal.ref_id)
        return {
            "ok": True,
            "change_id": proposal.ref_id,
            "request_ref": proposal.request_ref,
            "changeset_ref": proposal.changeset_ref,
            "state": "included_in_batch"
            if consumed
            else ("unavailable" if changeset is None else changeset.state),
            **({"included_in_change": consumed.batch_ref} if consumed else {}),
            "stale": stale,
            "preview": self.bounded_preview(proposal.preview),
            "issues": self.public_issues(revision.validation_errors_json if revision else []),
            "next": "docket_get_context" if stale else "docket_apply_change",
        }

    def apply(self, change_id: str) -> dict[str, Any]:
        principal = require_principal("commit")
        proposal = self.session.scalar(
            select(PreparedCommand).where(
                PreparedCommand.ref_id == change_id,
                PreparedCommand.principal_ref == principal.principal_ref,
            )
        )
        if proposal is None:
            raise DocketError(code="change_unavailable", message="Read your exact prepared change.")
        root = AgentRequestService(self.session).require(
            proposal.request_ref, permission="commit", allow_committed=True
        )
        outcome = self.session.get(NativeOutcome, root.ref_id)
        if outcome is not None:
            return {**outcome.receipt, "replayed": True}
        consumed = self.session.get(PreparedConsumption, proposal.ref_id)
        if consumed is not None:
            batch = self.session.scalar(
                select(PreparedCommand).where(
                    PreparedCommand.ref_id == consumed.batch_ref,
                    PreparedCommand.principal_ref == principal.principal_ref,
                )
            )
            parent = (
                self.session.scalar(
                    select(ChangeSet).where(
                        ChangeSet.ref_id == batch.changeset_ref,
                    )
                )
                if batch is not None
                else None
            )
            if parent is None or parent.state != "committed":
                raise DocketError(
                    code="batch_receipt_unavailable",
                    message="The retained batch receipt needs recovery.",
                )
            parent_outcome = self.session.get(NativeOutcome, batch.request_ref) if batch else None
            return {
                **(
                    parent.commit_receipt_json or (parent_outcome.receipt if parent_outcome else {})
                ),
                "included_in_change": consumed.batch_ref,
                "replayed": True,
            }
        changeset = self.session.scalar(
            select(ChangeSet)
            .where(
                ChangeSet.ref_id == proposal.changeset_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if changeset is None or self.fingerprint(changeset) != proposal.revision_hash:
            raise DocketError(
                code="prepared_change_stale",
                message="This exact proposal changed; read the retained change.",
            )
        batch_members: list[PreparedCommand] = []
        if proposal.command == "batch" and changeset.state != "committed":
            from docket.services.prepared_batches import members

            batch_members = members(self, proposal)
        admission = self._admission(
            root.ref_id, "apply", "docket_commit_changeset", proposal.revision_hash
        )
        result = ChangeSetAssemblyService(self.session).commit(
            request_ref=root.ref_id,
            request_key=root.request_key,
            assembly_operation_token=admission["assembly_operation_token"],
            assembly_argument_hash=proposal.revision_hash,
        )

        if batch_members and changeset.state == "committed":
            from docket.services.prepared_batches import consume

            consume(self, proposal, batch_members)
        return result

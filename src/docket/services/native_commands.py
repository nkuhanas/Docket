"""Compile business commands through the existing exact-revision ChangeSet engine."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import ChangeSet, PreparedCommand
from docket.schemas.assembly import StageChangesInput
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
    return {
        "kind": "timed",
        "start_local": values[0].replace(tzinfo=None).isoformat(),
        "end_local": values[1].replace(tzinfo=None).isoformat(),
        "timezone": timezone,
        "fold": shared_fold,
    }


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

    def create_event(
        self, command: CreateEvent, request_ref: str, request_key: str, *, prepare: bool = False
    ) -> dict[str, Any]:
        principal = require_principal("commit")
        AgentRequestService(self.session).require(
            request_ref, permission="commit", allow_committed=True
        )
        argument_hash = sha256_json(command.model_dump(mode="json"))
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
        context = NativeContextService(self.session, self.settings).require(
            command.context_id,
            "calendar",
        )
        if command.calendar_ref not in context.versions:
            raise DocketError(
                code="calendar_not_in_context",
                message="Read context for the selected calendar_ref.",
                details={"field": "calendar_ref"},
            )
        timing = event_timing(command.start, command.end, command.timezone)
        data = StageChangesInput.model_validate(
            {
                "request_ref": request_ref,
                "request_key": request_key,
                "expected_versions": {command.calendar_ref: context.versions[command.calendar_ref]},
                "assembly_scope": {
                    "resolved_intent": {"command": "create_event", "title": command.title},
                    "normalized_entry_types": ["scheduled_occurrence_entry"],
                    "selected_entry_ids": ["event"],
                    "target_refs": [command.calendar_ref],
                },
                "patch": {
                    "operations": [
                        {
                            "operation": "normalized_entry_upsert",
                            "entry": {
                                "import_entry_id": "event",
                                "entry_type": "scheduled_occurrence_entry",
                                "title": command.title,
                                "timing": timing,
                                "lane_ref": command.calendar_ref,
                                "location": command.location,
                                "description": command.notes,
                            },
                        }
                    ]
                },
            }
        )
        admission = self._admission(request_ref, "prepare", "docket_stage_changes", argument_hash)
        staged = ChangeSetAssemblyService(self.session).stage(
            data,
            assembly_operation_token=admission["assembly_operation_token"],
            assembly_argument_hash=argument_hash,
        )
        if staged.get("disposition") != "ready_to_commit":
            return {
                "ok": False,
                "error": {
                    "code": "event_needs_correction",
                    "message": "The event needs correction; inspect issues for affected fields.",
                    "details": staged,
                },
            }
        changeset = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.ref_id == staged["draft_ref"],
            )
        )
        assert changeset is not None
        proposal = PreparedCommand(
            principal_ref=principal.principal_ref,
            operation_id=command.operation_id,
            command="create_event",
            argument_hash=argument_hash,
            context_ref=context.ref_id,
            request_ref=request_ref,
            changeset_ref=changeset.ref_id,
            revision=changeset.current_revision,
            revision_hash=self.fingerprint(changeset),
            preview={
                "title": command.title,
                "start": command.start,
                "end": command.end,
                "timezone": command.timezone,
                "calendar_ref": command.calendar_ref,
                "location": command.location,
                "notes": command.notes,
            },
        )
        self.session.add(proposal)
        self.session.flush()
        return self.prepared(proposal) if prepare else self.apply(proposal.ref_id)

    @staticmethod
    def prepared(proposal: PreparedCommand) -> dict[str, Any]:
        return {
            "ok": True,
            "disposition": "prepared",
            "change_id": proposal.ref_id,
            "preview": proposal.preview,
            "next": "docket_apply_change",
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
        admission = self._admission(
            root.ref_id, "apply", "docket_commit_changeset", proposal.revision_hash
        )
        return ChangeSetAssemblyService(self.session).commit(
            request_ref=root.ref_id,
            request_key=root.request_key,
            assembly_operation_token=admission["assembly_operation_token"],
            assembly_argument_hash=proposal.revision_hash,
        )

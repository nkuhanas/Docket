import uuid
from datetime import date
from typing import Any, cast

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import canonical_record_key, sha256_json
from docket.domain.enums import CommandStatus, RecordStatus
from docket.domain.errors import DocketError, IdempotencyConflict, RecordNotFound, VersionConflict
from docket.models import AuditEvent, CommandRequest, Record, RecordSource
from docket.models.base import utc_now
from docket.schemas.records import (
    ArchiveRecordInput,
    RecordResult,
    RememberRecordInput,
    TermData,
    UpdateRecordInput,
)


def _validated_data(
    record_type: str, data: dict[str, Any]
) -> tuple[dict[str, Any], date | None, date | None]:
    if record_type == "term":
        term = TermData.model_validate(data)
        normalized = term.model_dump(mode="json")
        return normalized, term.start_date, term.end_date
    return data, None, None


def _replayed(result: dict[str, Any]) -> RecordResult:
    replayed = dict(result)
    replayed["disposition"] = "replayed_request"
    return RecordResult.model_validate(replayed)


def _validate_discord_source(request: RememberRecordInput) -> None:
    settings = get_settings()
    metadata = request.source.metadata
    expected = {
        "guild_id": settings.discord_guild_id,
        "channel_id": settings.chat_channel_id,
        "user_id": settings.operator_discord_user_id,
    }
    actual = {
        "guild_id": metadata.guild_id,
        "channel_id": metadata.channel_id,
        "user_id": metadata.user_id,
    }
    if actual != expected:
        raise DocketError(
            code="invalid_source_context",
            message="Discord record source does not match the configured operator context.",
        )


class RecordService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _start_command(
        self,
        *,
        request_key: str,
        operation_name: str,
        payload: dict[str, Any],
        actor_type: str,
        actor_id: str | None,
    ) -> tuple[CommandRequest, RecordResult | None]:
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
                raise IdempotencyConflict(request_key)
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                return existing, _replayed(existing.result)
            raise DocketError(
                code="request_in_progress",
                message="The request exists but has not completed successfully.",
                details={"request_key": request_key, "status": existing.status},
            )

        command = CommandRequest(
            request_key=request_key,
            operation_name=operation_name,
            input_sha256=input_sha256,
            actor_type=actor_type,
            actor_id=actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    @staticmethod
    def _finish_command(command: CommandRequest, result: RecordResult) -> None:
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()

    def remember(self, request: RememberRecordInput) -> RecordResult:
        _validate_discord_source(request)
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="docket_remember_record",
            payload=payload,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay

        canonical_identity = request.canonical_identity.model_dump(mode="json")
        request_data = request.data.model_dump(mode="json")
        canonical_key = canonical_record_key(request.record_type, canonical_identity)
        normalized_data, valid_from, valid_until = _validated_data(
            request.record_type, request_data
        )

        if request.record_type == "term":
            term = TermData.model_validate(normalized_data)
            if canonical_key != canonical_record_key(
                "term", {"institution": term.institution, "term_name": term.term_name}
            ):
                raise DocketError(
                    code="canonical_identity_mismatch",
                    message="Term identity must match the validated record data.",
                )

        record = self.session.scalar(
            select(Record).where(
                Record.record_type == request.record_type,
                Record.canonical_key == canonical_key,
            )
        )
        disposition = "matched_existing"
        if record is None:
            record = Record(
                record_type=request.record_type,
                canonical_key=canonical_key,
                schema_version=1,
                title=request.title,
                data=normalized_data,
                valid_from_date=valid_from,
                valid_until_date=valid_until,
                status=RecordStatus.ACTIVE.value,
            )
            self.session.add(record)
            self.session.flush()
            disposition = "created"

        source = request.source
        self.session.add(
            RecordSource(
                record_id=record.id,
                source_type=source.source_type,
                source_account_id=None,
                source_object_id=source.source_object_id,
                source_request_key=request.request_key,
                source_version=source.source_version,
                source_metadata=source.metadata.model_dump(mode="json"),
            )
        )
        self.session.add(
            AuditEvent(
                event_type="record.created" if disposition == "created" else "record.matched",
                entity_type="record",
                entity_id=record.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "record_type": record.record_type,
                    "canonical_key": record.canonical_key,
                    "version": record.version,
                    "data_sha256": sha256_json(record.data),
                },
            )
        )
        result = RecordResult(
            record_id=record.id,
            version=record.version,
            disposition=cast(Any, disposition),
            request_id=command.id,
        )
        self._finish_command(command, result)
        return result

    def get(self, record_id: uuid.UUID) -> Record:
        record = self.session.get(Record, record_id)
        if record is None:
            raise RecordNotFound(str(record_id))
        return record

    def search(
        self,
        *,
        record_type: str | None = None,
        query: str | None = None,
        status: RecordStatus | None = RecordStatus.ACTIVE,
        limit: int = 20,
    ) -> list[Record]:
        statement: Select[tuple[Record]] = select(Record)
        if record_type:
            statement = statement.where(Record.record_type == record_type)
        if status:
            statement = statement.where(Record.status == status.value)
        if query:
            statement = statement.where(Record.title.ilike(f"%{query}%"))
        statement = statement.order_by(Record.updated_at.desc()).limit(min(max(limit, 1), 100))
        return list(self.session.scalars(statement))

    def update(self, request: UpdateRecordInput) -> RecordResult:
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="docket_update_record",
            payload=payload,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay

        record = self.get(request.record_id)
        if record.version != request.expected_version:
            raise VersionConflict(str(record.id), request.expected_version, record.version)

        normalized_data, valid_from, valid_until = _validated_data(record.record_type, request.data)
        previous_hash = sha256_json(record.data)
        record.data = normalized_data
        record.valid_from_date = valid_from
        record.valid_until_date = valid_until
        record.version += 1
        self.session.add(
            AuditEvent(
                event_type="record.updated",
                entity_type="record",
                entity_id=record.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "previous_data_sha256": previous_hash,
                    "data_sha256": sha256_json(record.data),
                    "version": record.version,
                    "reason": request.reason,
                },
            )
        )
        result = RecordResult(
            record_id=record.id,
            version=record.version,
            disposition="updated",
            request_id=command.id,
        )
        self._finish_command(command, result)
        return result

    def archive(self, request: ArchiveRecordInput) -> RecordResult:
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="docket_archive_record",
            payload=payload,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay

        record = self.get(request.record_id)
        if record.version != request.expected_version:
            raise VersionConflict(str(record.id), request.expected_version, record.version)
        record.status = RecordStatus.ARCHIVED.value
        record.version += 1
        self.session.add(
            AuditEvent(
                event_type="record.archived",
                entity_type="record",
                entity_id=record.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={"version": record.version, "reason": request.reason},
            )
        )
        result = RecordResult(
            record_id=record.id,
            version=record.version,
            disposition="archived",
            request_id=command.id,
        )
        self._finish_command(command, result)
        return result


def serialize_record(record: Record) -> dict[str, Any]:
    return {
        "record_id": str(record.id),
        "record_type": record.record_type,
        "canonical_key": record.canonical_key,
        "schema_version": record.schema_version,
        "title": record.title,
        "status": record.status,
        "data": record.data,
        "version": record.version,
        "valid_from_date": record.valid_from_date.isoformat() if record.valid_from_date else None,
        "valid_until_date": record.valid_until_date.isoformat()
        if record.valid_until_date
        else None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }

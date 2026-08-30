from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import AttachmentManifest, OperatorUtteranceCapture
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.models import (
    AttachmentEvidence,
    EncryptedAttachmentBlob,
    OperatorUtterance,
    Source,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.schemas.authority import StatementInput
from docket.services.attachment_evidence import AttachmentCapture, AttachmentEvidenceService
from docket.services.deferred_ingress import DeferredIngressRunner
from docket.services.history import HistoryService
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService


def _request(
    *,
    message_id: str,
    attachment_id: str = "1542999000000000001",
    content: bytes | None = b"schedule bytes",
    ingest_error_code: str | None = None,
) -> OperatorUtteranceCapture:
    settings = get_settings()
    manifest = AttachmentManifest.model_validate(
        {
            "transport_attachment_ref": attachment_id,
            "filename": "schedule.png",
            "media_type": "image/png",
            "byte_size": len(content) if content is not None else 14,
            "received_at": datetime.now(UTC),
            "plaintext_base64": (
                base64.b64encode(content).decode("ascii") if content is not None else None
            ),
            "ingest_error_code": ingest_error_code,
        }
    )
    return OperatorUtteranceCapture(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=message_id,
        actor_id=settings.operator_discord_user_id,
        verbatim_text="Import this schedule as tracked context.",
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            f"{message_id}:0"
        ),
        attachments=[manifest],
    )


@pytest.mark.integration
def test_attachment_is_encrypted_bound_and_idempotent(session_factory) -> None:
    request = _request(message_id="1542999000000000010")
    with session_factory.begin() as session:
        created = ProvenanceService(session).capture_operator_utterance(request)
    with session_factory.begin() as session:
        replay = ProvenanceService(session).capture_operator_utterance(request)

    assert replay["ref"] == created["ref"]
    assert replay["attachments"] == created["attachments"]
    source_ref = created["attachments"][0]["ref"]
    assert created["attachments"][0] == {
        "ref": source_ref,
        "ingest_state": "available",
        "retention_disposition": "retained_encrypted",
        "content_hash": hashlib.sha256(b"schedule bytes").hexdigest(),
        "source_revision": 1,
        "untrusted_content": True,
    }

    with session_factory() as session:
        utterance = session.scalar(select(OperatorUtterance))
        evidence = session.scalar(select(AttachmentEvidence))
        source = session.scalar(select(Source))
        blob = session.scalar(select(EncryptedAttachmentBlob))
        assert utterance is not None
        assert evidence is not None
        assert source is not None
        assert blob is not None
        assert utterance.attachment_source_refs == [source_ref]
        assert evidence.operator_utterance_ref == utterance.ref_id
        assert source.ref_id == evidence.ref_id == source_ref
        assert b"schedule bytes" not in blob.ciphertext
        assert session.scalar(select(func.count(AttachmentEvidence.id))) == 1
        settings = get_settings()
        assert (
            AttachmentEvidenceService(
                session,
                encryption_key=settings.attachment_encryption_key(),
                encryption_key_ref=settings.attachment_encryption_key_ref,
                max_attachment_bytes=settings.attachment_max_bytes,
                max_total_bytes=settings.attachment_total_max_bytes,
            ).plaintext(source_ref)
            == b"schedule bytes"
        )
        history = HistoryService(session).get_entry(source_ref)
        assert history["entry"]["attachment"]["operator_utterance_ref"] == utterance.ref_id
        assert history["entry"]["attachment"]["content_hash"] == hashlib.sha256(
            b"schedule bytes"
        ).hexdigest()
        assert "plaintext_base64" not in str(history)

    request_without_manifest = request.model_copy(update={"attachments": []})
    with pytest.raises(IdempotencyConflict), session_factory.begin() as session:
        ProvenanceService(session).capture_operator_utterance(request_without_manifest)


@pytest.mark.integration
def test_attachment_statement_preserves_bounded_fragment_lineage(session_factory) -> None:
    request = _request(message_id="1542999000000000020", content=b"quiz row")
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        statement = StatementService(session).derive(
            captured["ref"],
            [
                StatementInput(
                    statement_kind="item_candidate",
                    subject_refs=[new_public_ref("ent")],
                    predicate="scheduled_item",
                    value_json={"title": "Quiz", "date": "2026-09-18"},
                    affected_fields=["title", "scheduled_on"],
                    interpretation_json={"interpretation_version": "fixture-v1"},
                    interpreter_version="fixture-v1",
                    source_ref=source_ref,
                    source_fragment_locator={
                        "page": 1,
                        "table": 1,
                        "row": 4,
                        "cell": [2, 3],
                    },
                    source_fragment_hash=hashlib.sha256(b"quiz row").hexdigest(),
                    extractor_identifier="fixture.schedule-table",
                    extractor_version="1.0.0",
                )
            ],
        )[0]
        statement_ref = statement.ref_id

    with session_factory() as session:
        evidence = session.scalar(select(AttachmentEvidence))
        assert evidence is not None
        assert evidence.derived_content_refs == [statement_ref]

    with pytest.raises(ValidationError, match="structural coordinates"):
        StatementInput.model_validate(
            {
                "statement_kind": "item_candidate",
                "subject_refs": [new_public_ref("ent")],
                "predicate": "scheduled_item",
                "value_json": {},
                "affected_fields": ["title"],
                "interpreter_version": "fixture-v1",
                "source_ref": source_ref,
                "source_fragment_locator": {"row": {"content": "raw source text"}},
                "extractor_identifier": "fixture.schedule-table",
                "extractor_version": "1.0.0",
            }
        )


@pytest.mark.integration
def test_pending_attachment_blocks_statement_and_mutation(session_factory) -> None:
    request = _request(message_id="1542999000000000030", content=None)
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        assert captured["attachments"][0]["ingest_state"] == "pending"
        with pytest.raises(DocketError) as error:
            StatementService(session).derive(
                captured["ref"],
                [
                    StatementInput(
                        statement_kind="item_candidate",
                        subject_refs=[new_public_ref("ent")],
                        predicate="scheduled_item",
                        value_json={},
                        affected_fields=["title"],
                        interpreter_version="fixture-v1",
                        source_ref=source_ref,
                        source_fragment_locator={"page": 1},
                        extractor_identifier="fixture.schedule-table",
                        extractor_version="1.0.0",
                    )
                ],
            )
        assert error.value.code == "attachment_evidence_unavailable"

    server = ProvenanceFastMCP("attachment-fail-closed", caller_profile="interactive")
    executed = False

    @server.tool(name="docket_commit_changeset")
    def commit_changeset(utterance_ref: str, request_key: str) -> dict[str, object]:
        nonlocal executed
        executed = True
        return {"ok": True, "ref": new_public_ref("chg"), "disposition": "committed"}

    with pytest.raises(ToolError, match="attachment_evidence_unavailable"):
        asyncio.run(
            server.call_tool(
                "docket_commit_changeset",
                {"utterance_ref": captured["ref"], "request_key": request.request_key},
            )
        )
    assert executed is False
    with session_factory() as session:
        invocation = session.scalar(
            select(ToolInvocation).order_by(ToolInvocation.started_at.desc())
        )
        assert invocation is not None
        assert invocation.transport_state == "completed"
        assert invocation.domain_state == "rejected"
        assert invocation.result_disposition == "attachment_evidence_unavailable"
        assert invocation.result_refs == [source_ref]


@pytest.mark.integration
def test_attachment_failure_is_terminal_and_records_no_plaintext(session_factory) -> None:
    request = _request(
        message_id="1542999000000000040",
        content=None,
        ingest_error_code="attachment_download_failed",
    )
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        assert captured["attachments"][0]["ingest_state"] == "failed"
        assert captured["attachments"][0]["retention_disposition"] == "metadata_only"
        assert captured["attachments"][0]["content_hash"] is None

    with session_factory() as session:
        assert session.scalar(select(func.count(EncryptedAttachmentBlob.id))) == 0

    available_retry = _request(
        message_id="1542999000000000040",
        content=b"x" * 14,
    )
    available_retry = available_retry.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "attachments": [
                available_retry.attachments[0].model_copy(
                    update={
                        "transport_attachment_ref": request.attachments[0].transport_attachment_ref,
                        "byte_size": request.attachments[0].byte_size,
                        "received_at": request.attachments[0].received_at,
                    }
                )
            ],
        }
    )
    with pytest.raises(IdempotencyConflict), session_factory.begin() as session:
        ProvenanceService(session).capture_operator_utterance(available_retry)


@pytest.mark.integration
def test_deferred_ingress_waits_for_durable_bytes_then_replays_exact_evidence(
    session_factory,
) -> None:
    settings = get_settings()
    received_at = datetime.now(UTC)
    pending = AttachmentCapture(
        transport_attachment_ref="1542999000000000051",
        filename="schedule.png",
        media_type="image/png",
        byte_size=14,
        received_at=received_at,
    )

    def ledger(session):
        return IngressLedgerService(
            session,
            identity=IngressIdentity(
                operator_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                chat_channel_id=settings.chat_channel_id,
                queue_channel_id=settings.queue_channel_id,
            ),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
            attachment_encryption_key=settings.attachment_encryption_key(),
            attachment_encryption_key_ref=settings.attachment_encryption_key_ref,
            attachment_max_bytes=settings.attachment_max_bytes,
            attachment_total_max_bytes=settings.attachment_total_max_bytes,
        )

    capture_arguments = {
        "actor_id": settings.operator_discord_user_id,
        "guild_id": settings.discord_guild_id,
        "channel_id": settings.chat_channel_id,
        "parent_channel_id": None,
        "message_id": "1542999000000000050",
        "reply_to_message_id": None,
        "verbatim_text": "Import this attachment.",
        "said_at": received_at,
    }
    with session_factory.begin() as session:
        captured = ledger(session).capture_message(**capture_arguments, attachments=[pending])

    adapter = FakeDiscordProjectionAdapter()
    runner = DeferredIngressRunner(session_factory, adapter)
    assert runner.run_once() is False
    assert adapter.deferred_ingress == []

    plaintext = b"x" * 14
    with session_factory.begin() as session:
        replay = ledger(session).capture_message(
            **capture_arguments,
            attachments=[
                AttachmentCapture(
                    transport_attachment_ref=pending.transport_attachment_ref,
                    filename=pending.filename,
                    media_type=pending.media_type,
                    byte_size=pending.byte_size,
                    received_at=pending.received_at,
                    plaintext=plaintext,
                )
            ],
        )
    assert replay["utterance_ref"] == captured["utterance_ref"]
    assert replay["attachments"][0]["ingest_state"] == "available"
    assert runner.run_once() is True
    payload = adapter.deferred_ingress[0]
    assert payload["utterance_ref"] == captured["utterance_ref"]
    assert payload["attachment_evidence"][0]["ref"] == replay["attachments"][0]["ref"]
    assert payload["attachment_evidence"][0]["content_hash"] == hashlib.sha256(
        plaintext
    ).hexdigest()
    assert base64.b64decode(
        payload["attachment_evidence"][0]["plaintext_base64"], validate=True
    ) == plaintext

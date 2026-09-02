from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from io import BytesIO

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import AttachmentManifest, OperatorUtteranceCapture
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.mcp.server import mcp
from docket.models import (
    AttachmentEvidence,
    ChangeSet,
    EncryptedAttachmentBlob,
    IntentSession,
    InterpretedStatement,
    Item,
    ItemSourceBinding,
    OperatorUtterance,
    Source,
    Task,
    TemporalBinding,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.schemas.authority import (
    CURRENT_IMPORT_AUTHORITY_STATEMENT,
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    OperatorChangeSetContent,
    StatementInput,
)
from docket.services.attachment_evidence import (
    PDF_TEXT_EXTRACTOR,
    AttachmentCapture,
    AttachmentEvidenceService,
    AttachmentTextService,
)
from docket.services.change_sets import ChangeSetService
from docket.services.deferred_ingress import DeferredIngressRunner
from docket.services.history import HistoryService
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService
from docket.services.tracked_context import TrackedContextService


def _request(
    *,
    message_id: str,
    attachment_id: str = "1542999000000000001",
    content: bytes | None = b"schedule bytes",
    ingest_error_code: str | None = None,
    filename: str = "schedule.png",
    media_type: str = "image/png",
) -> OperatorUtteranceCapture:
    settings = get_settings()
    manifest = AttachmentManifest.model_validate(
        {
            "transport_attachment_ref": attachment_id,
            "filename": filename,
            "media_type": media_type,
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


def _pdf_bytes(*pages: str) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_ref}
                )
            }
        )
        content = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _commit_tracked_content(
    session: Session,
    *,
    utterance_ref: str,
    idempotency_key: str,
    content: ChangeSetContent,
) -> list[str]:
    intent = IntentSession(
        conversation_ref=f"discord:{idempotency_key}",
        source_utterance_ref=utterance_ref,
        semantic_state="ready",
        commit_state="not_attempted",
    )
    session.add(intent)
    session.flush()
    service = ChangeSetService(
        session,
        handlers=TrackedContextService(session).handlers(),
    )
    changeset, _created = service.prepare(
        ChangeSetPrepare(
            intent_session_ref=intent.ref_id,
            expected_session_version=intent.version,
            idempotency_key=idempotency_key,
            content=content,
        )
    )
    _committed, receipt = service.commit(
        ChangeSetCommit(
            changeset_ref=changeset.ref_id,
            expected_version=changeset.version,
            idempotency_key=changeset.idempotency_key,
            authority_utterance_ref=utterance_ref,
        )
    )
    return receipt.affected_refs


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
def test_pdf_attachment_text_is_bounded_paginated_and_provenance_linked(
    session_factory,
) -> None:
    page_one = "ENGL 1134 Tuesday September 1 Introduction " + ("writing " * 80)
    page_two = "ENGL 1134 Thursday September 3 Rhetoric workshop"
    pdf = _pdf_bytes(page_one, page_two)
    request = _request(
        message_id="1542999000000000011",
        content=pdf,
        filename="engl-1134-schedule.pdf",
        media_type="application/pdf",
    )
    settings = get_settings()
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        evidence = AttachmentEvidenceService(
            session,
            encryption_key=settings.attachment_encryption_key(),
            encryption_key_ref=settings.attachment_encryption_key_ref,
            max_attachment_bytes=settings.attachment_max_bytes,
            max_total_bytes=settings.attachment_total_max_bytes,
        )
        extractor = AttachmentTextService(evidence)
        first = extractor.read_pdf_text(
            source_ref=source_ref,
            cursor=None,
            max_text_bytes=256,
            page_limit=1,
        )
        second = extractor.read_pdf_text(
            source_ref=source_ref,
            cursor=str(first["cursor"]),
            max_text_bytes=8192,
            page_limit=5,
        )

    assert first["source_ref"] == source_ref
    assert first["content_hash"] == hashlib.sha256(pdf).hexdigest()
    assert first["untrusted_content"] is True
    assert first["extractor_identifier"] == PDF_TEXT_EXTRACTOR
    assert first["extractor_version"]
    assert first["truncated"] is True
    assert len(json.dumps(first, separators=(",", ":")).encode("utf-8")) <= 16384
    assert first["cursor"]
    first_fragment = first["items"][0]
    assert first_fragment["source_fragment_locator"] == {
        "page": 1,
        "text_character_start": 0,
        "text_character_end": len(first_fragment["text"]),
    }
    assert first_fragment["source_fragment_hash"] == hashlib.sha256(
        first_fragment["text"].encode("utf-8")
    ).hexdigest()
    combined = "".join(
        str(fragment["text"]) for fragment in [*first["items"], *second["items"]]
    )
    assert "ENGL 1134 Tuesday September 1" in combined
    assert "ENGL 1134 Thursday September 3" in combined
    assert second["truncated"] is False


@pytest.mark.integration
def test_pdf_without_text_layer_fails_explicitly_without_claiming_ocr(session_factory) -> None:
    request = _request(
        message_id="1542999000000000012",
        content=_pdf_bytes(""),
        filename="scan.pdf",
        media_type="application/pdf",
    )
    settings = get_settings()
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        evidence = AttachmentEvidenceService(
            session,
            encryption_key=settings.attachment_encryption_key(),
            encryption_key_ref=settings.attachment_encryption_key_ref,
            max_attachment_bytes=settings.attachment_max_bytes,
            max_total_bytes=settings.attachment_total_max_bytes,
        )
        with pytest.raises(DocketError) as error:
            AttachmentTextService(evidence).read_pdf_text(
                source_ref=source_ref,
                cursor=None,
                max_text_bytes=8192,
                page_limit=3,
            )
    assert error.value.code == "attachment_pdf_no_text_layer"
    assert "OCR is not currently available" in error.value.message


@pytest.mark.integration
def test_pdf_attachment_read_tool_records_bounded_result_refs_not_content(
    session_factory,
) -> None:
    extracted_text = "ENGL 1134 schedule evidence"
    request = _request(
        message_id="1542999000000000013",
        content=_pdf_bytes(extracted_text),
        filename="engl-1134.pdf",
        media_type="application/pdf",
    )
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]

    result = asyncio.run(
        mcp.call_tool("docket_read_attachment_text", {"source_ref": source_ref})
    )
    assert isinstance(result, tuple) and len(result) == 2
    content, structured = result
    assert structured["source_ref"] == source_ref
    assert structured["items"][0]["text"] == extracted_text
    assert len(content[0].text.encode("utf-8")) <= 16384

    with session_factory() as session:
        invocation = session.scalar(
            select(ToolInvocation)
            .where(ToolInvocation.tool_name == "docket_read_attachment_text")
            .order_by(ToolInvocation.started_at.desc())
        )
        assert invocation is not None
        assert invocation.transport_state == "completed"
        assert invocation.domain_state == "succeeded"
        assert invocation.result_disposition is None
        assert invocation.result_refs == [source_ref]
        assert extracted_text not in repr(invocation.__dict__)


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
def test_one_interactive_call_completes_import_statement_provenance(session_factory) -> None:
    request = _request(message_id="1542999000000000023", content=b"lecture row")
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"]],
                "import_scope": {
                    "mode": "context_only",
                    "source_refs": [source_ref],
                },
                "tracked_context_changes": [
                    {
                        "mutation_type": "item_create",
                        "change_id": "lecture-11-9",
                        "action": "create",
                        "object_type": "item",
                        "affected_fields": ["title", "kind", "source_refs"],
                        "basis_refs": [captured["ref"]],
                        "create_spec": {
                            "title": "Lecture 11.9",
                            "kind": "academic.lecture_topic",
                            "source_refs": [source_ref],
                        },
                    }
                ],
            }
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=captured["ref"],
            request_key=request.request_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[
                StatementInput(
                    statement_kind="item_candidate",
                    subject_refs=[source_ref],
                    predicate="schedule_entry",
                    value_json={"title": "Lecture 11.9"},
                    affected_fields=["title"],
                    interpreter_version="fixture-v1",
                    source_ref=source_ref,
                    source_fragment_locator={"page": 1, "row": 3},
                    source_fragment_hash=hashlib.sha256(b"lecture row").hexdigest(),
                    extractor_identifier="fixture.schedule-table",
                    extractor_version="1.0.0",
                )
            ],
            relations=[],
            resolved_intent_json={"kind": "context_import"},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["disposition"] == "committed"
        item = session.scalar(select(Item))
        binding = session.scalar(select(ItemSourceBinding))
        statement = session.scalar(select(InterpretedStatement))
        assert item is not None and binding is not None and statement is not None
        assert statement.ref_id in item.basis_refs
        assert statement.ref_id in binding.basis_refs


@pytest.mark.integration
def test_one_interactive_call_resolves_explicit_import_authority_symbol(
    session_factory,
) -> None:
    request = _request(
        message_id="1542999000000000024",
        content=b"assignment row",
    ).model_copy(
        update={
            "verbatim_text": (
                "Import this assignment and create the task I need to complete."
            )
        }
    )
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        content = OperatorChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"]],
                "import_scope": {
                    "mode": "operator_explicit",
                    "source_refs": [source_ref],
                    "authorized_effects": ["item", "task"],
                },
                "tracked_context_changes": [
                    {
                        "mutation_type": "item_create",
                        "change_id": "assignment",
                        "action": "create",
                        "object_type": "item",
                        "affected_fields": ["title", "source_refs"],
                        "basis_refs": [captured["ref"]],
                        "create_spec": {
                            "title": "Assignment",
                            "source_refs": [source_ref],
                        },
                    },
                    {
                        "mutation_type": "task_create",
                        "change_id": "complete-assignment",
                        "action": "create",
                        "object_type": "task",
                        "affected_fields": ["item_ref", "task_state"],
                        "basis_refs": [captured["ref"]],
                        "create_spec": {
                            "item_change_id": "assignment",
                            "title": "Complete Assignment",
                            "source_refs": [source_ref],
                        },
                    },
                ],
            }
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=captured["ref"],
            request_key=request.request_key,
            actor_id=get_settings().operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[
                StatementInput(
                    statement_kind="item_candidate",
                    subject_refs=[source_ref],
                    predicate="schedule_entry",
                    value_json={"title": "Assignment"},
                    affected_fields=["title"],
                    interpreter_version="fixture-v1",
                    source_ref=source_ref,
                    source_fragment_locator={"page": 1, "row": 6},
                    source_fragment_hash=hashlib.sha256(b"assignment row").hexdigest(),
                    extractor_identifier="fixture.schedule-table",
                    extractor_version="1.0.0",
                ),
            ],
            relations=[],
            resolved_intent_json={"kind": "explicit_context_import"},
            blocking_clarifications=[],
            content=content.to_internal(),
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["disposition"] == "committed"
        changeset = session.scalar(select(ChangeSet))
        assert changeset is not None
        assert changeset.import_scope_json is not None
        authority_refs = changeset.import_scope_json["authority_statement_refs"]
        assert len(authority_refs) == 1
        assert authority_refs[0].startswith("stm_")
        assert CURRENT_IMPORT_AUTHORITY_STATEMENT not in str(changeset.import_scope_json)
        authority_statement = session.scalar(
            select(InterpretedStatement).where(
                InterpretedStatement.ref_id == authority_refs[0]
            )
        )
        assert authority_statement is not None
        assert authority_statement.source_ref is None
        assert authority_statement.interpretation_json["compiler"] == (
            "operator_import_scope"
        )


@pytest.mark.integration
def test_source_fragment_binding_replays_import_without_duplicate_context(
    session_factory,
) -> None:
    request = _request(message_id="1542999000000000025", content=b"midterm row")
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        statement = StatementService(session).derive(
            captured["ref"],
            [
                StatementInput(
                    statement_kind="item_candidate",
                    subject_refs=[source_ref],
                    predicate="schedule_entry",
                    value_json={"title": "Midterm", "date": "2026-09-18"},
                    affected_fields=["title", "scheduled_on"],
                    interpretation_json={"interpretation_version": "fixture-v1"},
                    interpreter_version="fixture-v1",
                    source_ref=source_ref,
                    source_fragment_locator={"page": 1, "table": 1, "row": 4},
                    source_fragment_hash=hashlib.sha256(b"midterm row").hexdigest(),
                    extractor_identifier="fixture.schedule-table",
                    extractor_version="1.0.0",
                )
            ],
        )[0]

        content = ChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"], statement.ref_id],
                "import_scope": {
                    "mode": "context_only",
                    "source_refs": [source_ref],
                },
                "tracked_context_changes": [
                    {
                        "mutation_type": "item_create",
                        "change_id": "midterm",
                        "action": "create",
                        "object_type": "item",
                        "affected_fields": ["title", "kind", "source_refs"],
                        "basis_refs": [captured["ref"], statement.ref_id],
                        "create_spec": {
                            "title": "Midterm",
                            "kind": "academic.exam",
                            "source_refs": [source_ref],
                        },
                    },
                    {
                        "mutation_type": "temporal_binding_create",
                        "change_id": "midterm-date",
                        "action": "create",
                        "object_type": "temporal_binding",
                        "affected_fields": ["subject_ref", "role", "temporal_value"],
                        "basis_refs": [captured["ref"], statement.ref_id],
                        "create_spec": {
                            "subject_change_id": "midterm",
                            "role": "scheduled_on",
                            "temporal_value": {
                                "kind": "date",
                                "date": "2026-09-18",
                                "timezone": "America/Los_Angeles",
                            },
                            "source_refs": [source_ref],
                        },
                    },
                ],
            }
        )

        affected_runs: list[list[str]] = []
        for index in (1, 2):
            affected_runs.append(
                _commit_tracked_content(
                    session,
                    utterance_ref=captured["ref"],
                    idempotency_key=f"attachment-import:{index}",
                    content=content,
                )
            )

    with session_factory() as session:
        items = list(session.scalars(select(Item)))
        bindings = list(session.scalars(select(ItemSourceBinding)))
        times = list(session.scalars(select(TemporalBinding)))
        assert len(items) == len(bindings) == len(times) == 1
        assert bindings[0].item_ref == items[0].ref_id
        assert bindings[0].source_ref == source_ref
        assert bindings[0].semantic_role == "schedule_entry"
        assert affected_runs[0] == affected_runs[1]
        context = TrackedContextService(session).item_context(items[0].ref_id)
        assert context["source_bindings"] == [
            {
                "source_ref": source_ref,
                "source_revision_key": bindings[0].source_revision_key,
                "source_fragment_locator": {"page": 1, "table": 1, "row": 4},
                "semantic_role": "schedule_entry",
            }
        ]


@pytest.mark.integration
def test_identical_bytes_in_distinct_uploads_do_not_merge_items(session_factory) -> None:
    plaintext = b"same schedule row"
    item_refs: list[str] = []
    source_refs: list[str] = []

    for index in (1, 2):
        request = _request(
            message_id=f"154299900000000002{6 + index}",
            attachment_id=f"154299900000000010{index}",
            content=plaintext,
        )
        with session_factory.begin() as session:
            captured = ProvenanceService(session).capture_operator_utterance(request)
            source_ref = captured["attachments"][0]["ref"]
            source_refs.append(source_ref)
            statement = StatementService(session).derive(
                captured["ref"],
                [
                    StatementInput(
                        statement_kind="item_candidate",
                        subject_refs=[source_ref],
                        predicate="schedule_entry",
                        value_json={"title": "Quiz"},
                        affected_fields=["title"],
                        interpreter_version="fixture-v1",
                        source_ref=source_ref,
                        source_fragment_locator={"page": 1, "table": 1, "row": 2},
                        source_fragment_hash=hashlib.sha256(plaintext).hexdigest(),
                        extractor_identifier="fixture.schedule-table",
                        extractor_version="1.0.0",
                    )
                ],
            )[0]
            content = ChangeSetContent.model_validate(
                {
                    "basis_refs": [captured["ref"], statement.ref_id],
                    "import_scope": {
                        "mode": "context_only",
                        "source_refs": [source_ref],
                    },
                    "tracked_context_changes": [
                        {
                            "mutation_type": "item_create",
                            "change_id": f"quiz-{index}",
                            "action": "create",
                            "object_type": "item",
                            "affected_fields": ["title", "kind", "source_refs"],
                            "basis_refs": [captured["ref"], statement.ref_id],
                            "create_spec": {
                                "title": "Quiz",
                                "kind": "academic.quiz",
                                "source_refs": [source_ref],
                            },
                        }
                    ],
                }
            )
            item_refs.append(
                _commit_tracked_content(
                    session,
                    utterance_ref=captured["ref"],
                    idempotency_key=f"distinct-upload:{index}",
                    content=content,
                )[0]
            )

    with session_factory() as session:
        assert len(set(source_refs)) == 2
        assert len(set(item_refs)) == 2
        assert session.scalar(select(func.count(Item.id))) == 2
        assert session.scalar(select(func.count(ItemSourceBinding.id))) == 2


@pytest.mark.integration
def test_attachment_import_scope_blocks_source_broadening_without_operator_statement(
    session_factory,
) -> None:
    request = _request(message_id="1542999000000000031", content=b"assignment row")
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
        statements = StatementService(session).derive(
            captured["ref"],
            [
                StatementInput(
                    statement_kind="item_candidate",
                    subject_refs=[source_ref],
                    predicate="schedule_entry",
                    value_json={"title": "Problem Set 4"},
                    affected_fields=["title"],
                    interpreter_version="fixture-v1",
                    source_ref=source_ref,
                    source_fragment_locator={"page": 1, "row": 5},
                    source_fragment_hash=hashlib.sha256(b"assignment row").hexdigest(),
                    extractor_identifier="fixture.schedule-table",
                    extractor_version="1.0.0",
                ),
                StatementInput(
                    statement_kind="operator_intent",
                    subject_refs=[source_ref],
                    predicate="import_effect_authority",
                    value_json={"authorized_effects": ["item", "task"]},
                    affected_fields=["import_scope"],
                    interpreter_version="fixture-v1",
                ),
            ],
        )
        source_statement_ref = statements[0].ref_id
        authority_statement_ref = statements[1].ref_id

    item_change = {
        "mutation_type": "item_create",
        "change_id": "problem-set-4",
        "action": "create",
        "object_type": "item",
        "affected_fields": ["title", "kind", "source_refs"],
        "basis_refs": [captured["ref"], source_statement_ref],
        "create_spec": {
            "title": "Problem Set 4",
            "kind": "academic.assignment",
            "source_refs": [source_ref],
        },
    }
    task_change = {
        "mutation_type": "task_create",
        "change_id": "complete-problem-set-4",
        "action": "create",
        "object_type": "task",
        "affected_fields": ["item_ref", "task_state"],
        "basis_refs": [captured["ref"], source_statement_ref],
        "create_spec": {
            "item_change_id": "problem-set-4",
            "title": "Complete Problem Set 4",
            "source_refs": [source_ref],
        },
    }

    with session_factory.begin() as session:
        no_scope = ChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"], source_statement_ref],
                "tracked_context_changes": [item_change],
            }
        )
        intent = IntentSession(
            conversation_ref="discord:import:no-scope",
            source_utterance_ref=captured["ref"],
            semantic_state="ready",
            commit_state="not_attempted",
        )
        session.add(intent)
        session.flush()
        service = ChangeSetService(
            session,
            handlers=TrackedContextService(session).handlers(),
        )
        changeset, _created = service.prepare(
            ChangeSetPrepare(
                intent_session_ref=intent.ref_id,
                expected_session_version=intent.version,
                idempotency_key="attachment-import:no-scope",
                content=no_scope,
            )
        )
        assert {error["code"] for error in changeset.validation_errors} == {
            "import_scope_required"
        }

    with session_factory.begin() as session:
        context_only = ChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"], source_statement_ref],
                "import_scope": {
                    "mode": "context_only",
                    "source_refs": [source_ref],
                },
                "tracked_context_changes": [item_change, task_change],
            }
        )
        intent = IntentSession(
            conversation_ref="discord:import:context-only",
            source_utterance_ref=captured["ref"],
            semantic_state="ready",
            commit_state="not_attempted",
        )
        session.add(intent)
        session.flush()
        changeset, _created = ChangeSetService(
            session,
            handlers=TrackedContextService(session).handlers(),
        ).prepare(
            ChangeSetPrepare(
                intent_session_ref=intent.ref_id,
                expected_session_version=intent.version,
                idempotency_key="attachment-import:context-only",
                content=context_only,
            )
        )
        assert {error["code"] for error in changeset.validation_errors} == {
            "import_effect_outside_scope"
        }

    with session_factory.begin() as session:
        explicit = ChangeSetContent.model_validate(
            {
                "basis_refs": [
                    captured["ref"],
                    source_statement_ref,
                    authority_statement_ref,
                ],
                "import_scope": {
                    "mode": "operator_explicit",
                    "source_refs": [source_ref],
                    "authorized_effects": ["item", "task"],
                    "authority_statement_refs": [authority_statement_ref],
                },
                "tracked_context_changes": [
                    item_change,
                    {
                        **task_change,
                        "basis_refs": [
                            captured["ref"],
                            source_statement_ref,
                            authority_statement_ref,
                        ],
                    },
                ],
            }
        )
        affected = _commit_tracked_content(
            session,
            utterance_ref=captured["ref"],
            idempotency_key="attachment-import:operator-explicit",
            content=explicit,
        )
        assert len(affected) == 2
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(func.count(Task.id))) == 1


@pytest.mark.integration
def test_attachment_item_requires_exact_fragment_statement(session_factory) -> None:
    request = _request(message_id="1542999000000000026", content=b"orphan row")
    with session_factory.begin() as session:
        captured = ProvenanceService(session).capture_operator_utterance(request)
        source_ref = captured["attachments"][0]["ref"]
    with session_factory.begin() as session:
        content = ChangeSetContent.model_validate(
            {
                "basis_refs": [captured["ref"]],
                "import_scope": {
                    "mode": "context_only",
                    "source_refs": [source_ref],
                },
                "tracked_context_changes": [
                    {
                        "mutation_type": "item_create",
                        "change_id": "orphan",
                        "action": "create",
                        "object_type": "item",
                        "affected_fields": ["title", "source_refs"],
                        "basis_refs": [captured["ref"]],
                        "create_spec": {
                            "title": "Orphan import",
                            "source_refs": [source_ref],
                        },
                    }
                ],
            }
        )
        intent = IntentSession(
            conversation_ref="discord:attachment-import:missing-fragment",
            source_utterance_ref=captured["ref"],
            semantic_state="ready",
            commit_state="not_attempted",
        )
        session.add(intent)
        session.flush()
        service = ChangeSetService(
            session,
            handlers=TrackedContextService(session).handlers(),
        )
        changeset, _created = service.prepare(
            ChangeSetPrepare(
                intent_session_ref=intent.ref_id,
                expected_session_version=intent.version,
                idempotency_key="attachment-import:missing-fragment",
                content=content,
            )
        )
        assert changeset.state == "draft"
        assert changeset.validation_errors == [
            {
                "code": "import_effect_source_fragment_required",
                "details": {"change_id": "orphan"},
            }
        ]


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

    result = asyncio.run(
        server.call_tool(
            "docket_commit_changeset",
            {"utterance_ref": captured["ref"], "request_key": request.request_key},
        )
    )
    assert isinstance(result, tuple)
    payload = result[1]
    assert payload["ok"] is False
    assert payload["disposition"] == "attachment_evidence_unavailable"
    assert payload["error"]["code"] == "attachment_evidence_unavailable"
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
def test_attachment_failure_is_terminal_and_records_no_plaintext(
    session_factory,
) -> None:
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
    with session_factory.begin() as session:
        replayed = ProvenanceService(session).capture_operator_utterance(available_retry)
        assert replayed["disposition"] == "replayed_request"
        assert replayed["attachments"][0]["ingest_state"] == "failed"
        assert replayed["attachments"][0]["content_hash"] is None

    with session_factory() as session:
        assert session.scalar(select(func.count(EncryptedAttachmentBlob.id))) == 0


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

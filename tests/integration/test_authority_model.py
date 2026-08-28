import uuid

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    AuditEvent,
    ChangeSet,
    ChangeSetRevision,
    Conflict,
    Decision,
    Entity,
    IntentSession,
    IntentTurn,
    InterpretedStatement,
    OperatorUtterance,
)
from docket.schemas.authority import (
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ConflictOpen,
    IntentSessionOpen,
    IntentTurnAppend,
    StatementInput,
    StatementRelationInput,
)
from docket.services.change_sets import ChangeSetService
from docket.services.conflicts import ConflictService
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService


def _capture_utterance(session, *, message_id: str, text: str) -> str:
    settings = get_settings()
    request = OperatorUtteranceCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "actor_id": settings.operator_discord_user_id,
            "verbatim_text": text,
            "request_key": (
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                f"{message_id}:0"
            ),
        }
    )
    return str(ProvenanceService(session).capture_operator_utterance(request)["ref"])


def _statement(
    *,
    value: str,
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> StatementInput:
    return StatementInput.model_validate(
        {
            "statement_kind": "fact_assertion",
            "subject_refs": ["ent_01M13MZZZZZZZZZZZZZZZZZZZZ"],
            "predicate": "office_hours",
            "value_json": value,
            "affected_fields": ["office_hours"],
            "effective_from": effective_from,
            "effective_to": effective_to,
            "interpretation_json": {"source": "test"},
            "interpreter_version": "test-v1",
        }
    )


@pytest.mark.integration
def test_intent_session_and_turn_survive_as_durable_exact_state(session_factory) -> None:
    with session_factory.begin() as session:
        utterance_ref = _capture_utterance(
            session,
            message_id="1542799000000000000",
            text="Chris has Monday office hours and works in Building 14.",
        )
        intent_session, created = IntentSessionService(session).open(
            IntentSessionOpen(source_utterance_ref=utterance_ref)
        )
        session_ref = intent_session.ref_id
        assert created is True
        intent_session, turn = IntentSessionService(session).append_turn(
            IntentTurnAppend(
                intent_session_ref=session_ref,
                utterance_ref=utterance_ref,
                statements=[
                    _statement(value="Monday 3 PM"),
                    StatementInput.model_validate(
                        {
                            "statement_kind": "fact_assertion",
                            "subject_refs": ["ent_01M13MZZZZZZZZZZZZZZZZZZZZ"],
                            "predicate": "office_location",
                            "value_json": "Building 14",
                            "affected_fields": ["office_location"],
                            "interpreter_version": "test-v1",
                        }
                    ),
                ],
                response_disposition="no_response",
                resolved_intent_json={"kind": "registry_context"},
                blocking_clarifications=[
                    {"blocking": True, "question": "Which Chris?"}
                ],
            )
        )
        turn_ref = turn.ref_id
        assert intent_session.state == "needs_clarification"
        assert len(turn.statement_refs) == 2

    with session_factory() as session:
        restored = IntentSessionService(session).get(session_ref)
        assert restored.state == "needs_clarification"
        assert restored.version == 2
        restored_turn = session.scalar(
            select(IntentTurn).where(IntentTurn.ref_id == turn_ref)
        )
        assert restored_turn is not None
        assert restored_turn.utterance_ref == utterance_ref
        assert restored_turn.response_disposition == "no_response"
        assert (
            session.scalar(select(func.count()).select_from(InterpretedStatement)) == 2
        )

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        restored_turn = session.scalar(
            select(IntentTurn).where(IntentTurn.ref_id == turn_ref)
        )
        assert restored_turn is not None
        restored_turn.response_disposition = "pending"


@pytest.mark.integration
def test_statement_derivation_allows_zero_and_replays_without_mutation(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        harmless_ref = _capture_utterance(
            session,
            message_id="1542799000000000001",
            text="lol",
        )
        assert StatementService(session).derive(harmless_ref, []) == []
        assert StatementService(session).derive(harmless_ref, []) == []
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OperatorUtterance)) == 1
        assert session.scalar(select(func.count()).select_from(InterpretedStatement)) == 0


@pytest.mark.integration
def test_nonoverlap_and_explicit_supersession_do_not_open_conflict(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        prior_utterance = _capture_utterance(
            session,
            message_id="1542799000000000002",
            text="Alice lived in Dorm A during Fall 2026.",
        )
        incoming_utterance = _capture_utterance(
            session,
            message_id="1542799000000000003",
            text="Alice lives in Dorm B during Fall 2027.",
        )
        prior = StatementService(session).derive(
            prior_utterance,
            [
                _statement(
                    value="Dorm A",
                    effective_from="2026-08-01",
                    effective_to="2026-12-31",
                )
            ],
        )[0]
        incoming = StatementService(session).derive(
            incoming_utterance,
            [
                _statement(
                    value="Dorm B",
                    effective_from="2027-08-01",
                    effective_to="2027-12-31",
                )
            ],
        )[0]
        request = ConflictOpen(
            subject_refs=prior.subject_refs,
            affected_fields=prior.affected_fields,
            prior_statement_refs=[prior.ref_id],
            incoming_statement_refs=[incoming.ref_id],
            conflicting_effects_json={"prior": "Dorm A", "incoming": "Dorm B"},
        )
        with pytest.raises(DocketError) as exc_info:
            ConflictService(session).open(request)
        assert exc_info.value.code == "conflict_not_applicable"

        overlapping = StatementService(session).derive(
            incoming_utterance,
            [_statement(value="Dorm C", effective_from="2026-09-01")],
        )[0]
        StatementService(session).relate(
            StatementRelationInput(
                source_statement_ref=overlapping.ref_id,
                target_statement_ref=prior.ref_id,
                relation_kind="supersedes",
            )
        )
        request = request.model_copy(
            update={"incoming_statement_refs": [overlapping.ref_id]}
        )
        with pytest.raises(DocketError) as exc_info:
            ConflictService(session).open(request)
        assert exc_info.value.code == "conflict_not_applicable"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Conflict)) == 0


@pytest.mark.integration
def test_conflict_resolution_commits_through_one_immutable_changeset(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        prior_utterance = _capture_utterance(
            session,
            message_id="1542799000000000004",
            text="Chris office hours are Monday at 3.",
        )
        incoming_utterance = _capture_utterance(
            session,
            message_id="1542799000000000005",
            text="Chris office hours are Wednesday at 2.",
        )
        prior = StatementService(session).derive(
            prior_utterance, [_statement(value="Monday 3 PM")]
        )[0]
        incoming = StatementService(session).derive(
            incoming_utterance, [_statement(value="Wednesday 2 PM")]
        )[0]
        conflict = ConflictService(session).open(
            ConflictOpen(
                subject_refs=prior.subject_refs,
                affected_fields=["office_hours"],
                prior_statement_refs=[prior.ref_id],
                incoming_statement_refs=[incoming.ref_id],
                conflicting_effects_json={
                    "prior": prior.value_json,
                    "incoming": incoming.value_json,
                },
            )
        )
        assert conflict.status == "open"

        resolution_utterance = _capture_utterance(
            session,
            message_id="1542799000000000006",
            text="Wednesday at 2 replaces Monday.",
        )
        intent_session, _created = IntentSessionService(session).open(
            IntentSessionOpen(source_utterance_ref=resolution_utterance)
        )
        changeset, created = ChangeSetService(session).prepare(
            ChangeSetPrepare.model_validate(
                {
                    "intent_session_ref": intent_session.ref_id,
                    "expected_session_version": intent_session.version,
                    "idempotency_key": "discord:test:conflict-resolution:1",
                    "content": {
                        "basis_refs": [resolution_utterance],
                        "expected_versions": {conflict.ref_id: 1},
                        "resolution_changes": [
                            {
                                "change_id": "resolve-office-hours",
                                "action": "update",
                                "object_type": "conflict_resolution",
                                "object_ref": conflict.ref_id,
                                "affected_fields": ["office_hours"],
                                "basis_refs": [resolution_utterance],
                                "payload": {
                                    "expected_version": 1,
                                    "authority_utterance_ref": resolution_utterance,
                                    "resolution": "resolved_supersession",
                                    "chosen_interpretation": {
                                        "office_hours": "Wednesday 2 PM"
                                    },
                                    "statements_superseded": [prior.ref_id],
                                    "statements_retained": [incoming.ref_id],
                                    "effective_scope": {},
                                    "canonical_effects": [],
                                },
                            }
                        ],
                    },
                }
            )
        )
        assert created is True
        assert changeset.state == "validated"
        assert intent_session.state == "ready"
        committed, affected_refs = ChangeSetService(session).commit(
            ChangeSetCommit(
                changeset_ref=changeset.ref_id,
                expected_version=changeset.version,
                idempotency_key=changeset.idempotency_key,
                authority_utterance_ref=resolution_utterance,
            )
        )
        changeset_ref = committed.ref_id
        assert committed.state == "committed"
        assert conflict.ref_id in affected_refs

    with session_factory() as session:
        conflict = session.scalar(select(Conflict))
        decision = session.scalar(
            select(Decision).where(Decision.decision_kind == "conflict_resolution")
        )
        changeset = session.scalar(
            select(ChangeSet).where(ChangeSet.ref_id == changeset_ref)
        )
        intent_session = session.scalar(select(IntentSession))
        assert conflict is not None and decision is not None
        assert changeset is not None and intent_session is not None
        assert conflict.status == "resolved_supersession"
        assert conflict.resolution_decision_ref == decision.ref_id
        assert decision.basis_refs == [resolution_utterance, conflict.ref_id]
        assert changeset.state == "committed"
        assert intent_session.state == "committed"
        assert intent_session.committed_changeset_ref == changeset.ref_id
        assert session.scalar(select(func.count()).select_from(ChangeSetRevision)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 1

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        changeset = session.scalar(
            select(ChangeSet).where(ChangeSet.ref_id == changeset_ref)
        )
        assert changeset is not None
        changeset.registry_changes = []


@pytest.mark.integration
def test_interactive_clarification_resumes_commits_and_replays(session_factory) -> None:
    with session_factory.begin() as session:
        subject = Entity(
            entity_class="person",
            canonical_name="Chris",
            normalized_name="chris",
            status="active",
            attributes={},
            authority="operator",
        )
        session.add(subject)
        session.flush()
        prior_utterance = _capture_utterance(
            session,
            message_id="1542799000000000007",
            text="Chris office hours are Monday at 3.",
        )
        incoming_utterance = _capture_utterance(
            session,
            message_id="1542799000000000008",
            text="Chris office hours are Wednesday at 2.",
        )
        prior_input = _statement(value="Monday 3 PM").model_copy(
            update={"subject_refs": [subject.ref_id]}
        )
        incoming_input = _statement(value="Wednesday 2 PM").model_copy(
            update={"subject_refs": [subject.ref_id]}
        )
        prior = StatementService(session).derive(prior_utterance, [prior_input])[0]
        incoming = StatementService(session).derive(
            incoming_utterance, [incoming_input]
        )[0]
        conflict = ConflictService(session).open(
            ConflictOpen(
                subject_refs=[subject.ref_id],
                affected_fields=["office_hours"],
                prior_statement_refs=[prior.ref_id],
                incoming_statement_refs=[incoming.ref_id],
                conflicting_effects_json={
                    "prior": prior.value_json,
                    "incoming": incoming.value_json,
                },
            )
        )
        clarification_utterance = _capture_utterance(
            session,
            message_id="1542799000000000009",
            text="Resolve Chris's office-hours conflict.",
        )
        settings = get_settings()
        first_key = (
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            "1542799000000000009:0"
        )
        service = InteractiveAuthorityService(session)
        first = service.process_turn(
            utterance_ref=clarification_utterance,
            request_key=first_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"conflict_ref": conflict.ref_id},
            blocking_clarifications=[
                {
                    "blocking": True,
                    "question": "Does Wednesday replace Monday?",
                }
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert first["state"] == "needs_clarification"
        session_ref = first["ref"]
        session_version = first["intent_session"]["version"]

        resolution_utterance = _capture_utterance(
            session,
            message_id="1542799000000000010",
            text="Wednesday at 2 replaces Monday.",
        )
        second_key = (
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            "1542799000000000010:0"
        )
        content = ChangeSetContent.model_validate({
            "basis_refs": [resolution_utterance],
            "expected_versions": {conflict.ref_id: conflict.version},
            "resolution_changes": [
                {
                    "change_id": "resolve-office-hours-interactively",
                    "action": "update",
                    "object_type": "conflict_resolution",
                    "object_ref": conflict.ref_id,
                    "affected_fields": ["office_hours"],
                    "basis_refs": [resolution_utterance],
                    "payload": {
                        "expected_version": conflict.version,
                        "authority_utterance_ref": resolution_utterance,
                        "resolution": "resolved_supersession",
                        "chosen_interpretation": {
                            "office_hours": "Wednesday 2 PM"
                        },
                        "statements_superseded": [prior.ref_id],
                        "statements_retained": [incoming.ref_id],
                        "effective_scope": {},
                        "canonical_effects": [],
                    },
                }
            ],
        })
        second = service.process_turn(
            utterance_ref=resolution_utterance,
            request_key=second_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=session_ref,
            expected_session_version=session_version,
            statements=[],
            relations=[],
            resolved_intent_json={"conflict_ref": conflict.ref_id, "replace": True},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert second["state"] == "committed"
        replay = service.process_turn(
            utterance_ref=resolution_utterance,
            request_key=second_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=session_ref,
            expected_session_version=session_version,
            statements=[],
            relations=[],
            resolved_intent_json={"conflict_ref": conflict.ref_id, "replace": True},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert replay["disposition"] == "replayed_request"
        assert replay["ref"] == second["ref"]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IntentSession)) == 1
        assert session.scalar(select(func.count()).select_from(IntentTurn)) == 2
        assert session.scalar(select(func.count()).select_from(ChangeSet)) == 1
        conflict = session.scalar(select(Conflict))
        assert conflict is not None and conflict.status == "resolved_supersession"


@pytest.mark.integration
def test_open_conflict_blocks_whole_changeset_until_blocked_effect_removed(
    session_factory,
) -> None:
    applied: list[str] = []

    def apply_fact(_session, _changeset, change):
        applied.append(change.change_id)
        return []

    with session_factory.begin() as session:
        blocked_subject = Entity(
            entity_class="person",
            canonical_name="Blocked Chris",
            normalized_name="blocked chris",
            status="active",
            attributes={},
            authority="operator",
        )
        clean_subject = Entity(
            entity_class="person",
            canonical_name="Clean Alice",
            normalized_name="clean alice",
            status="active",
            attributes={},
            authority="operator",
        )
        session.add_all([blocked_subject, clean_subject])
        session.flush()
        prior_utterance = _capture_utterance(
            session,
            message_id="1542799000000000011",
            text="Chris office hours are Monday.",
        )
        incoming_utterance = _capture_utterance(
            session,
            message_id="1542799000000000012",
            text="Chris office hours are Wednesday.",
        )
        prior = StatementService(session).derive(
            prior_utterance,
            [
                _statement(value="Monday").model_copy(
                    update={"subject_refs": [blocked_subject.ref_id]}
                )
            ],
        )[0]
        incoming = StatementService(session).derive(
            incoming_utterance,
            [
                _statement(value="Wednesday").model_copy(
                    update={"subject_refs": [blocked_subject.ref_id]}
                )
            ],
        )[0]
        ConflictService(session).open(
            ConflictOpen(
                subject_refs=[blocked_subject.ref_id],
                affected_fields=["office_hours"],
                prior_statement_refs=[prior.ref_id],
                incoming_statement_refs=[incoming.ref_id],
                conflicting_effects_json={"prior": "Monday", "incoming": "Wednesday"},
            )
        )
        command_utterance = _capture_utterance(
            session,
            message_id="1542799000000000013",
            text="Set both facts.",
        )
        settings = get_settings()
        first_key = (
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            "1542799000000000013:0"
        )
        service = InteractiveAuthorityService(
            session,
            handlers={"fact": apply_fact},
        )

        def fact_change(change_id: str, subject_ref: str, basis_ref: str) -> dict:
            return {
                "change_id": change_id,
                "action": "create",
                "object_type": "fact",
                "create_spec": {
                    "subject_refs": [subject_ref],
                    "predicate": "test_fact",
                    "value": change_id,
                },
                "affected_fields": ["office_hours"],
                "basis_refs": [basis_ref],
            }

        first_content = ChangeSetContent.model_validate({
            "basis_refs": [command_utterance],
            "registry_changes": [
                fact_change("blocked-fact", blocked_subject.ref_id, command_utterance),
                fact_change("clean-fact", clean_subject.ref_id, command_utterance),
            ],
        })
        first = service.process_turn(
            utterance_ref=command_utterance,
            request_key=first_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "two_facts"},
            blocking_clarifications=[],
            content=first_content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert first["state"] == "needs_clarification"
        assert applied == []
        assert any(
            item["code"] == "open_conflict"
            for item in first["changeset"]["validation_errors"]
        )

        revision_utterance = _capture_utterance(
            session,
            message_id="1542799000000000014",
            text="Remove the blocked mutation and proceed with the other fact.",
        )
        second_key = (
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
            "1542799000000000014:0"
        )
        second_content = ChangeSetContent.model_validate({
            "basis_refs": [revision_utterance],
            "registry_changes": [
                fact_change("clean-fact", clean_subject.ref_id, revision_utterance)
            ],
        })
        second = service.process_turn(
            utterance_ref=revision_utterance,
            request_key=second_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=first["intent_session"]["ref"],
            expected_session_version=first["intent_session"]["version"],
            statements=[],
            relations=[],
            resolved_intent_json={"kind": "one_fact"},
            blocking_clarifications=[],
            content=second_content,
            changeset_ref=first["changeset"]["ref"],
            expected_changeset_version=first["changeset"]["version"],
        )
        assert second["state"] == "committed"
        assert applied == ["clean-fact"]

    with session_factory() as session:
        changeset = session.scalar(select(ChangeSet))
        assert changeset is not None and changeset.state == "committed"
        assert [change["change_id"] for change in changeset.registry_changes] == [
            "clean-fact"
        ]
        assert session.scalar(select(func.count()).select_from(ChangeSetRevision)) == 2

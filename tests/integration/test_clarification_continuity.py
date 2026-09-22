import base64
from copy import deepcopy

import pytest
from sqlalchemy import func, select
from test_attachment_evidence import _request
from test_changeset_assembly import _admit
from test_field_evidence import _assert_committed, _fixture

from docket.domain.public_refs import new_public_ref
from docket.models import (
    CanonicalEvent,
    ClarificationReply,
    IntentSession,
    Operation,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    ProjectionDelivery,
    RequestFieldEvidence,
)
from docket.schemas.assembly import StageChangesInput
from docket.schemas.authority import SemanticOptionDraft
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.provenance import ProvenanceService
from docket.services.semantic_options import SemanticOptionService


def _prompt(session):
    original, _, lane, operations, fields, *_ = _fixture(session)
    content = {"basis_refs": [original.ref_id], "event_changes": [], "lane_changes": []}
    for patch in operations:
        action = patch["action"]
        content[
            "event_changes" if action["object_type"] == "canonical_event" else "lane_changes"
        ].append(action)
    intent = IntentSession(
        conversation_ref=original.conversation_ref,
        source_utterance_ref=original.ref_id,
        semantic_state="needs_clarification",
        commit_state="not_attempted",
    )
    session.add(intent)
    session.flush()
    prompt = SemanticOptionService(session).persist_prompt(
        utterance=original,
        intent_session=intent,
        question="How long should each meeting be?",
        drafts=[
            SemanticOptionDraft.model_validate(
                {
                    "option_id": "one-hour",
                    "selection_authority_ref": original.ref_id,
                    "content": content,
                    "field_evidence": fields["bindings"],
                }
            )
        ],
    )
    delivery = session.scalar(
        select(ProjectionDelivery).where(
            ProjectionDelivery.projection_ref == prompt.ref_id,
        )
    )
    delivery.status = "delivered"
    delivery.external_message_ref = (
        original.source_message_ref.rsplit(":", 1)[0] + ":1542999000000000790"
    )
    session.flush()
    return original, lane, operations, fields, intent, prompt


def _answer(session, *, text="1 hour", explicit=False, message_id="1542999000000000801"):
    request = _request(message_id=message_id).model_copy(
        update={
            "attachments": [],
            "verbatim_text": text,
            "reply_to_message_id": "1542999000000000790" if explicit else None,
        }
    )
    captured = ProvenanceService(session).capture_operator_utterance(request)
    utterance = session.scalar(
        select(OperatorUtterance).where(
            OperatorUtterance.ref_id == captured["ref"],
        )
    )
    return utterance, captured, request


def _stage_answer(session, answer, original, lane, operations, fields, *, alter=None):
    operations = deepcopy(operations)
    for patch in operations:
        patch["action"]["basis_refs"] = [
            answer.ref_id if ref == original.ref_id else ref
            for ref in patch["action"]["basis_refs"]
        ]
    if alter:
        alter(operations)
    trace = new_public_ref("trace")
    service = ChangeSetAssemblyService(session)

    def token(ordinal, tool):
        return _admit(
            session,
            utterance=answer,
            trace_ref=trace,
            call_id=f"reply-{ordinal}",
            ordinal=ordinal,
            tool_name=tool,
            argument_hash=f"{ordinal:064x}",
        )

    result = service.stage(
        StageChangesInput.model_validate(
            {
                "utterance_ref": answer.ref_id,
                "request_key": answer.request_key,
                "assembly_scope": {
                    "resolved_intent": {"intent": "add seven one-hour meetings"},
                    "allowed_mutation_types": [
                        "canonical_event_create",
                        "lane_routing_decision_create",
                    ],
                    "source_refs": [fields["bindings"][0]["source_ref"]],
                    "target_refs": [lane.ref_id],
                },
                "patch": {"operations": [*operations, fields]},
            }
        ),
        assembly_operation_token=token(1, "docket_stage_changes"),
        assembly_argument_hash=f"{1:064x}",
    )

    def commit(ordinal=2):
        return service.commit(
            utterance_ref=answer.ref_id,
            request_key=answer.request_key,
            assembly_operation_token=token(ordinal, "docket_commit_changeset"),
            assembly_argument_hash=f"{ordinal:064x}",
        )

    return result, commit


@pytest.mark.parametrize("explicit", [True, False])
def test_answer_reuses_original_image_and_commits_once(session, explicit):
    original, lane, operations, fields, intent, _ = _prompt(session)
    answer, captured, request = _answer(session, explicit=explicit)
    assert captured["reply_binding"]["intent_session_ref"] == intent.ref_id
    assert answer.attachment_source_refs == []  # never rewrite original attachment ownership
    source = fields["bindings"][0]["source_ref"]
    assert captured["retained_attachment_summaries"][0]["ref"] == source
    assert base64.b64decode(captured["retained_attachments"][0]["plaintext_base64"]).startswith(
        b"\x89PNG"
    )
    replay = ProvenanceService(session).capture_operator_utterance(request)
    assert replay["reply_binding"] == captured["reply_binding"]
    staged, commit = _stage_answer(session, answer, original, lane, operations, fields)
    assert staged["ok"], staged
    result = commit()
    assert result["ok"], result
    _assert_committed(session, lane)
    assert commit(3)["ok"]
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 7
    assert session.scalar(select(func.count()).select_from(Operation)) == 7
    proof = session.scalars(select(RequestFieldEvidence)).one().proof_json
    assert set(proof["originating_utterances"]) == {answer.ref_id}
    assert set(proof["evidence_utterances"]) == {answer.ref_id, original.ref_id}
    assert session.scalar(select(func.count()).select_from(IntentSession)) == 1


@pytest.mark.parametrize("change", ["duration", "date", "count"])
def test_selected_duration_cannot_expand_to_other_effects(session, change):
    original, lane, operations, fields, _, _ = _prompt(session)
    answer, _, _ = _answer(session)
    fields = deepcopy(fields)
    if change == "count":
        fields["bindings"][0]["targets"].append({
            "change_id": "eighth-meeting",
            "field_path": "create_spec.event_spec.location", "match": "prefix",
        })

    def alter(patches):
        timing = patches[0]["action"]["create_spec"]["event_spec"]["timing"]
        if change == "duration":
            timing["end_local"] = "2026-09-17T22:50:00"
        elif change == "date":
            timing["start_local"] = "2026-09-18T19:50:00"
            timing["end_local"] = "2026-09-18T20:50:00"
        else:
            extra = deepcopy(patches[0])
            extra["action"]["change_id"] = "eighth-meeting"
            extra["action"]["create_spec"]["event_spec"]["timing"].update(
                start_local="2026-12-04T19:50:00", end_local="2026-12-04T20:50:00",
            )
            patches.append(extra)
            route = deepcopy(patches[1])
            route["action"]["change_id"] = "eighth-route"
            route["action"]["create_spec"]["event_change_id"] = "eighth-meeting"
            patches.append(route)

    staged, commit = _stage_answer(session, answer, original, lane, operations, fields, alter=alter)
    assert "clarification_effect_mismatch" in str(staged), staged.get("diagnostic_sample")
    assert not commit()["ok"]
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 0


def test_unrelated_message_and_intervening_context_do_not_borrow_images(session):
    _prompt(session)
    _, unrelated, _ = _answer(session, text="What meetings are tomorrow?")
    assert not unrelated["reply_binding"]
    assert unrelated["retained_attachments"] == []
    _, answer, _ = _answer(session, message_id="1542999000000000802")
    assert not answer["reply_binding"]


def test_disabled_historical_prompt_does_not_make_current_answer_ambiguous(session):
    original, _, _, _, intent, prompt = _prompt(session)
    disabled = OperatorProjection(
        projection_kind="clarification", operator_ref=original.actor_ref,
        primary_public_ref=intent.ref_id, intent_session_ref=intent.ref_id,
        semantic_content={"render": {"options": [
            {"visible_text": "Create new object."}, {"visible_text": "Create new object."},
        ]}}, visible_text="Old indistinguishable question", render_schema_version=1,
        render_sha256="a" * 64, component_sha256="b" * 64,
        basis_refs=[original.ref_id],
    )
    session.add(disabled)
    session.flush()
    session.add(ProjectionDelivery(
        projection_id=disabled.id, projection_ref=disabled.ref_id, transport="discord",
        destination_ref=original.conversation_ref, status="delivered",
        last_error_code="semantic_options_indistinguishable",
    ))
    session.flush()
    _, captured, _ = _answer(session)
    assert captured["reply_binding"]["projection_ref"] == prompt.ref_id


def test_closed_prompt_reply_recovers_committed_request_without_duplicate_events(session):
    original, lane, operations, fields, intent, _ = _prompt(session)
    answer, _, _ = _answer(session)
    _, commit = _stage_answer(session, answer, original, lane, operations, fields)
    assert commit()["ok"]
    second, captured, _ = _answer(session, explicit=True, message_id="1542999000000000802")
    assert captured["reply_binding"]["intent_session_ref"] == intent.ref_id
    result, _ = _stage_answer(session, second, original, lane, operations, fields)
    assert result["disposition"] == "already_committed", result
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 7


def test_continuation_binding_is_immutable(session):
    _prompt(session)
    answer, _, _ = _answer(session)
    reply = session.get(ClarificationReply, answer.ref_id)
    assert reply is not None
    reply.evidence_utterance_refs = []
    with pytest.raises(ValueError, match="ClarificationReply is immutable"):
        session.flush()


def test_unbound_capture_replay_does_not_adopt_a_later_prompt(session):
    original, _, _, _, _, prompt = _prompt(session)
    answer, captured, request = _answer(session, text="Unrelated request")
    assert captured["reply_binding"] is None
    assert session.get(ClarificationReply, answer.ref_id) is None
    # An existing audited message is not reinterpreted during a transport retry.
    replay = ProvenanceService(session).capture_operator_utterance(request)
    assert replay["reply_binding"] is None
    assert original.ref_id in prompt.basis_refs


def test_unrelated_source_cannot_enter_a_clarification_reply(session):
    original, lane, operations, fields, _, _ = _prompt(session)
    answer, _, _ = _answer(session, explicit=True)
    unrelated = ProvenanceService(session).capture_operator_utterance(_request(
        message_id="1542999000000000820", attachment_id="1542999000000000821",
    ))
    fields = deepcopy(fields)
    fields["bindings"][0]["source_ref"] = unrelated["attachments"][0]["ref"]
    staged, commit = _stage_answer(session, answer, original, lane, operations, fields)
    assert "source_bound_to_original_utterance" in str(staged)
    assert not commit()["ok"]
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 0


def test_option_pins_supporting_field_evidence(session):
    _, _, _, fields, _, _ = _prompt(session)
    option = session.scalars(select(PersistedSemanticOption)).one()
    assert (
        option.execution_preconditions_json["field_evidence"][0]["value"]
        == fields["bindings"][0]["value"]
    )


def test_button_selection_compiles_original_attachment_and_commits_once(session):
    import uuid
    from datetime import UTC, datetime, timedelta

    from docket.config import get_settings
    from docket.internal_api.schemas import SemanticOptionSelection
    from docket.schemas.authority import ChangeSetContent
    from docket.security import issue_semantic_option_token
    from docket.services.interactive_authority import InteractiveAuthorityService

    original, lane, _, _, _, _ = _prompt(session)
    settings = get_settings()
    option = session.scalars(select(PersistedSemanticOption)).one()
    token = issue_semantic_option_token(
        option_row_id=option.id,
        actor_id=settings.operator_discord_user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
    )
    request = SemanticOptionSelection(
        request_id=uuid.uuid4(),
        discord_interaction_id="1542999000000000888",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id="1542999000000000790",
        option_token=token,
        responded_at=datetime.now(UTC),
    )
    selection = SemanticOptionService(session).capture_selection(request)

    def execute():
        return InteractiveAuthorityService(session).process_turn(
            utterance_ref=selection["utterance_ref"],
            request_key=selection["request_key"],
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=selection["intent_session_ref"],
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"selected_option": "one-hour"},
            blocking_clarifications=[],
            content=ChangeSetContent.model_validate(selection["compiled_content"]),
            changeset_ref=None,
            expected_changeset_version=None,
            semantic_request_ref=selection["semantic_request_ref"],
            authority_scope_hash=selection["authority_scope_hash"],
            precondition_hash=selection["precondition_hash"],
        )

    result = execute()
    assert result["ok"], result
    _assert_committed(session, lane)
    replay = SemanticOptionService(session).capture_selection(request)
    assert replay["commit_state"] == "committed"
    assert not replay["execution_ready"]
    assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 7
    proof = session.scalars(select(RequestFieldEvidence)).one().proof_json
    assert original.ref_id in proof["evidence_utterances"]

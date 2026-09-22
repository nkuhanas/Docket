from copy import deepcopy

import pytest
from sqlalchemy import select
from test_clean_semantic_options import _draft, _utterance
from test_field_evidence import _fixture

from docket.domain.errors import DocketError
from docket.models import IntentSession, OperatorProjection, PersistedSemanticOption
from docket.schemas.authority import SemanticOptionDraft
from docket.services.clarification_labels import calendar_choice
from docket.services.semantic_options import SemanticOptionService, require_distinct_choices


@pytest.mark.parametrize("evidence_bound", [True, False])
def test_duration_choices_show_actual_events_and_differ(session, evidence_bound):
    utterance, _, _, operations, fields, _, _, _ = _fixture(session)
    content = {"basis_refs": [utterance.ref_id], "event_changes": [], "lane_changes": []}
    for patch in operations:
        action = patch["action"]
        content["event_changes" if action["object_type"] == "canonical_event"
                else "lane_changes"].append(action)
    second = deepcopy(content)
    from datetime import datetime, timedelta

    for event in second["event_changes"]:
        timing = event["create_spec"]["event_spec"]["timing"]
        timing["end_local"] = (datetime.fromisoformat(timing["end_local"])
                               + timedelta(minutes=30)).isoformat()
    intent = IntentSession(conversation_ref=utterance.conversation_ref,
                           source_utterance_ref=utterance.ref_id,
                           semantic_state="needs_clarification", commit_state="not_attempted")
    session.add(intent)
    session.flush()
    drafts = [SemanticOptionDraft.model_validate({
            "option_id": name, "selection_authority_ref": utterance.ref_id, "content": value,
            "field_evidence": fields["bindings"] if evidence_bound else [],
        }) for name, value in (("one-hour", content), ("ninety-minutes", second))]
    if not evidence_bound:
        with pytest.raises(DocketError) as error:
            SemanticOptionService(session).persist_prompt(
                utterance=utterance, intent_session=intent, question="How long?", drafts=drafts,
            )
        assert error.value.code == "clarification_field_evidence_required"
        assert not list(session.scalars(select(OperatorProjection)))
        return
    prompt = SemanticOptionService(session).persist_prompt(
        utterance=utterance, intent_session=intent, question="How long should each meeting be?",
        drafts=drafts,
    )
    options = prompt.semantic_content["render"]["options"]
    assert [o["button_label"] for o in options] == ["60 minutes each", "90 minutes each"]
    assert options[0]["visible_text"] != options[1]["visible_text"]
    assert "2026-09-17 19:50\u201320:50" in options[0]["visible_text"]
    assert "2026-09-17 19:50\u201321:20" in options[1]["visible_text"]
    assert "America/Los_Angeles" in options[0]["visible_text"]
    assert "Room 113" in options[0]["visible_text"]
    assert "new object" not in prompt.visible_text
    assert len(prompt.visible_text) < 4000
    assert len(list(session.scalars(select(PersistedSemanticOption)))) == 2


def test_prompt_cannot_persist_indistinguishable_options(session):
    utterance = _utterance("1542799000000000661")
    session.add(utterance)
    session.flush()
    intent = IntentSession(conversation_ref=utterance.conversation_ref,
                           source_utterance_ref=utterance.ref_id)
    session.add(intent)
    session.flush()
    draft = _draft(utterance.ref_id)
    other = draft.model_copy(update={"option_id": "same-again"})
    with pytest.raises(DocketError) as error:
        SemanticOptionService(session).persist_prompt(
            utterance=utterance, intent_session=intent, question="Which?", drafts=[draft, other],
        )
    assert error.value.code == "semantic_options_indistinguishable"
    assert not list(session.scalars(select(OperatorProjection)))


def test_existing_ambiguous_prompt_cannot_be_selected():
    projection = OperatorProjection(semantic_content={"render": {"options": [
        {"visible_text": "Create new object."}, {"visible_text": "Create new object."},
    ]}})
    with pytest.raises(DocketError) as error:
        require_distinct_choices(projection)
    assert error.value.code == "semantic_options_indistinguishable"


def test_calendar_summary_does_not_hide_other_effects():
    assert calendar_choice({"preference_changes": [{"anything": "else"}]}) is None

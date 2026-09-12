import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_changeset_assembly import _admit, _utterance

from docket.domain.public_refs import new_public_ref
from docket.models import (
    Affiliation,
    AttentionCase,
    AttentionCaseRevision,
    CalendarLane,
    CaseItem,
    ChangeSet,
    ChangeSetRevision,
    Entity,
    Fact,
    IdentityHandle,
    Item,
    Preference,
    ProviderAccount,
    Relationship,
    ReminderPlan,
    SenderIdentityEmail,
    Task,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.schemas.authority import ChangeSetContent
from docket.services.canonical_patch_previews import capture_canonical_patch_preview
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.changeset_diff import bounded_details, canonical_patch_preview_diff


def _base(session):
    utterance = _utterance("1542799000000000712")
    session.add(utterance)
    session.flush()
    item = Item(title="Homework 2", description="Private unrelated description",
                created_by_changeset_ref=new_public_ref("chg"), basis_refs=[utterance.ref_id])
    session.add(item)
    session.flush()
    return utterance, item


def _action(utterance, row, object_type, mutation, action, *, payload=None, create_spec=None):
    return {"change_id": "change", "action": action, "mutation_type": mutation,
            "object_type": object_type, "object_ref": row.ref_id,
            "affected_fields": ["state"], "basis_refs": [utterance.ref_id],
            **({"payload": payload} if payload is not None else {}),
            **({"create_spec": create_spec} if create_spec is not None else {})}


def _snapshot(session, utterance, row, action, group="tracked_context_changes"):
    return capture_canonical_patch_preview(session, ChangeSetContent.model_validate({
        "basis_refs": [utterance.ref_id], "expected_versions": {row.ref_id: row.version},
        group: [action],
    }))


def _diff(snapshot, *, mutation_types=(), entry_types=()):
    revision = ChangeSetRevision(compiler_manifest_json={"canonical_patch_preview": snapshot},
                                 normalized_entries_json=[], compiled_action_ownership_json=[])
    return canonical_patch_preview_diff(
        revision, mutation_types=list(mutation_types), entry_types=list(entry_types),
    )


def test_item_diff_captures_only_changed_fields_preserves_null_and_large_detail(session):
    utterance, item = _base(session)
    item.description = "é🙂" * 3_000
    session.flush()
    action = _action(utterance, item, "item", "item_modify", "update",
                     payload={"title": "Correct homework", "description": None})
    snapshot = _snapshot(session, utterance, item, action)
    effect = snapshot["effects"][0]
    assert effect["before"] == {"title": "Homework 2", "description": item.description}
    assert effect["after"] == {"title": "Correct homework", "description": None}
    assert effect["expected_version"] == effect["observed_version"] == 1
    assert item.title == "Homework 2"
    assert str(item.id) not in json.dumps(snapshot)
    assert "basis_refs" not in json.dumps(snapshot)
    rows = _diff(snapshot)
    assert {tuple(row["field_path"]) for row in rows} == {("title",), ("description",)}
    assert any(row["after_present"] and row["after"] is None for row in rows)
    fragments = bounded_details(rows)
    assert any("detail_fragment" in row for row in fragments)
    assert all(len(json.dumps(row, ensure_ascii=False).encode()) < 5_000 for row in fragments)
    assert _diff(snapshot, mutation_types=["task_modify"]) == []
    assert _diff(snapshot, entry_types=["scheduled_occurrence_entry"]) == []


def test_task_reopen_preview_retains_actual_previous_completion(session):
    utterance, item = _base(session)
    instant = datetime(2026, 9, 11, 13, tzinfo=UTC)
    task = Task(item_ref=item.ref_id, title="Submit homework", task_state="completed",
                completed_at=instant, created_by_changeset_ref=new_public_ref("chg"))
    session.add(task)
    session.flush()
    action = _action(utterance, task, "task", "task_modify", "update",
                     payload={"task_state": "not_started", "completed_at": None})
    effect = _snapshot(session, utterance, task, action)["effects"][0]
    assert effect["before"] == {"task_state": "completed", "completed_at": instant.isoformat()}
    assert effect["after"] == {"task_state": "not_started", "completed_at": None}
    assert task.task_state == "completed"
    assert "Private unrelated" not in json.dumps(effect)


def test_same_draft_parent_dependency_is_not_an_invented_public_ref(session):
    utterance, item = _base(session)
    action = _action(utterance, item, "item", "item_modify", "update",
                     payload={"parent_item_change_id": "new-parent"})
    effect = _snapshot(session, utterance, item, action)["effects"][0]
    assert effect["before"] == {"parent_item_ref": None}
    assert effect["after"] == {"parent_item_ref": {"from_change_id": "new-parent"}}


@pytest.mark.parametrize("kind", [
    "item", "task", "temporal_binding", "reminder_plan", "temporal_calendar_projection",
    "entity", "preference", "calendar_lane", "affiliation", "relationship", "fact",
])
def test_retraction_previews_match_each_domains_actual_lifecycle(session, kind):
    utterance, item = _base(session)
    provenance = {"created_by_changeset_ref": new_public_ref("chg")}
    entity = Entity(entity_kind="person", display_name="Fixture", normalized_name="fixture",
                    **provenance)
    account = ProviderAccount(provider="google", external_account_id="preview-fixture",
                              capabilities=["google_calendar"])
    session.add_all([entity, account])
    session.flush()
    temporal = TemporalBinding(subject_ref=item.ref_id, role="due_by", temporal_value={
        "kind": "date", "date": "2026-09-12", "timezone": "America/Los_Angeles",
    }, **provenance)
    lane = CalendarLane(account_id=account.id, lane="fixture", display_name="Fixture",
                        color_hex="#3367D6", **provenance)
    session.add_all([temporal, lane])
    session.flush()
    candidates = {
        "item": item,
        "task": Task(item_ref=item.ref_id, title="Task", **provenance),
        "temporal_binding": temporal,
        "reminder_plan": ReminderPlan(
            subject_ref=temporal.ref_id, delivery_channels=["docket_queue"],
            lead_seconds=[3600], **provenance),
        "temporal_calendar_projection": TemporalCalendarProjection(
            temporal_binding_ref=temporal.ref_id, lane_ref=lane.ref_id,
            display_policy={"kind": "all_day_marker", "transparency": "transparent"}, **provenance),
        "entity": entity,
        "preference": Preference(preference_key="preview-fixture", policy_kind="behavior",
                                 target_type="global", policy_text="Fixture", **provenance),
        "calendar_lane": lane,
        "affiliation": Affiliation(subject_entity_id=entity.id, organization_entity_id=entity.id,
                                   **provenance),
        "relationship": Relationship(subject_entity_id=entity.id, object_entity_id=entity.id,
                                     **provenance),
        "fact": Fact(subject_ref=item.ref_id, predicate="fixture", value_json=True, **provenance),
    }
    row = candidates[kind]
    session.add(row)
    session.flush()
    group = ("preference_changes" if kind == "preference" else "lane_changes"
             if kind == "calendar_lane" else "registry_changes"
             if kind in {"entity", "affiliation", "relationship", "fact"}
             else "tracked_context_changes")
    action = _action(utterance, row, kind, f"{kind}_retract", "retract")
    effect = _snapshot(session, utterance, row, action, group)["effects"][0]
    field = ("enabled" if kind in {"calendar_lane", "temporal_calendar_projection"}
             else "status" if kind in {"preference", "affiliation", "relationship", "fact"}
             else "canonical_status")
    assert effect["before"] == {field: True if field == "enabled" else "active"}
    assert effect["after"] == {field: False if field == "enabled" else "retracted"}
    assert getattr(row, field) == effect["before"][field]


def test_supersession_previews_retirement_not_overwrite_of_original_time(session):
    utterance, item = _base(session)
    old = {"kind": "date", "date": "2026-09-12", "timezone": "America/Los_Angeles"}
    row = TemporalBinding(subject_ref=item.ref_id, role="due_by", temporal_value=old,
                          created_by_changeset_ref=new_public_ref("chg"))
    session.add(row)
    session.flush()
    action = _action(utterance, row, "temporal_binding", "temporal_binding_supersede", "supersede",
                     create_spec={"subject_ref": item.ref_id, "role": "due_by",
                                  "temporal_value": {**old, "date": "2026-09-13"}})
    effect = _snapshot(session, utterance, row, action)["effects"][0]
    assert effect["before"] == {"canonical_status": "active"}
    assert effect["after"] == {"canonical_status": "historical"}
    assert effect["replacement_in_staged_input"] is True
    assert row.temporal_value == old


def test_identity_binding_preview_uses_public_refs_and_no_provenance_chains(session):
    utterance, _item = _base(session)
    entity = Entity(entity_kind="person", display_name="Fixture", normalized_name="fixture",
                    created_by_changeset_ref=new_public_ref("chg"))
    session.add(entity)
    session.flush()
    handle = IdentityHandle(handle_type="email_address", value="fixture@example.com",
                            normalized_value="fixture@example.com", entity_id=entity.id,
                            status="bound", binding_rule="explicit_entity_ref")
    session.add(handle)
    session.flush()
    action = _action(utterance, handle, "identity_binding", "identity_binding_unbind", "unbind")
    effect = _snapshot(session, utterance, handle, action, "registry_changes")["effects"][0]
    assert effect["before"]["entity_ref"] == entity.ref_id
    assert effect["after"] == {"entity_ref": None, "binding_rule": None, "status": "unbound"}
    assert str(entity.id) not in json.dumps(effect)
    assert str(handle.id) not in json.dumps(effect)
    assert "fixture@example.com" not in json.dumps(effect)


def test_sender_association_diff_shows_set_change_without_mutation(session):
    utterance, _item = _base(session)
    sender = IdentityHandle(handle_type="sender_label", value="Fixture", normalized_value="fixture")
    email = IdentityHandle(handle_type="email_address", value="fixture@example.com",
                           normalized_value="fixture@example.com")
    session.add_all([sender, email])
    session.flush()
    session.add(SenderIdentityEmail(sender_identity_handle_id=sender.id,
                                     email_identity_handle_id=email.id,
                                     created_by_changeset_ref=new_public_ref("chg")))
    session.flush()
    action = _action(utterance, sender, "identity_handle", "identity_handle_modify", "update",
                     payload={"remove_associated_email_ref": email.ref_id})
    effect = _snapshot(session, utterance, sender, action, "registry_changes")["effects"][0]
    assert effect["before"] == {"associated_email_refs": [email.ref_id]}
    assert effect["after"] == {"associated_email_refs": []}
    assert session.scalar(select(SenderIdentityEmail)).status == "active"


def test_stage_review_uses_immutable_canonical_values_across_pages(session, monkeypatch):
    utterance, item = _base(session)
    action = _action(utterance, item, "item", "item_modify", "update",
                     payload={"title": "Correct homework"})
    trace = new_public_ref("trace")

    def admit(tool, ordinal):
        return _admit(session, utterance=utterance, trace_ref=trace, call_id=f"preview-{ordinal}",
                      ordinal=ordinal, tool_name=f"docket_{tool}", argument_hash="a" * 64)

    service = ChangeSetAssemblyService(session)
    result = service.stage(StageChangesInput(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        expected_versions={item.ref_id: item.version}, assembly_scope={
            "resolved_intent": {"kind": "fixture"}, "allowed_mutation_types": ["item_modify"],
            "target_refs": [item.ref_id],
        }, patch={"operations": [{"operation": "action_upsert", "action": action}]},
    ), assembly_operation_token=admit("stage_changes", 1), assembly_argument_hash="a" * 64)
    assert result["disposition"] == "ready_to_commit", result
    draft = session.scalar(select(ChangeSet))
    snapshot = deepcopy(draft.compiler_manifest_json["canonical_patch_preview"])
    # Optional review after unrelated canonical changes still shows the old snapshot.
    item.title = "Another request's change"
    item.version += 1
    session.commit()
    session.expire_all()

    def never_capture(*args, **kwargs):
        raise AssertionError("Review must not recapture live canonical values")

    monkeypatch.setattr("docket.services.changeset_assembly.capture_canonical_patch_preview",
                        never_capture)
    rows, cursor, ordinal = [], None, 2
    while True:
        page = service.review(ReviewChangesInput(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
            view="diff", limit=1, cursor=cursor,
        ), assembly_operation_token=admit("review_changeset", ordinal),
            assembly_argument_hash="a" * 64)
        assert page["canonical_target_diff_basis"] == "canonical_staging_snapshot"
        rows.extend(page["items"])
        ordinal += 1
        cursor = page.get("cursor")
        if cursor is None:
            break
    effect = next(row for row in rows if row["subject_kind"] == "canonical_target_effect"
                  and row.get("field_path") == ["title"])
    assert effect["before"] == "Homework 2" and effect["after"] == "Correct homework"
    assert "Another request" not in json.dumps(rows)
    assert "Private unrelated" not in json.dumps(rows)
    revision = session.scalar(select(ChangeSetRevision))
    assert revision.compiler_manifest_json["canonical_patch_preview"] == snapshot


def test_uncompiled_and_missing_targets_do_not_claim_before_after(session):
    assert capture_canonical_patch_preview(session, None)["available"] is False
    utterance, item = _base(session)
    action = _action(utterance, item, "item", "item_retract", "retract")
    action["object_ref"] = new_public_ref("item")
    effect = _snapshot(session, utterance, item, action)["effects"][0]
    assert effect["available"] is False
    assert effect["reason"] == "canonical_target_unavailable"
    assert "before" not in effect and "after" not in effect


@pytest.mark.parametrize("outcome", ["resolved", "keep_open", "suppressed", "cancelled"])
def test_case_preview_distinguishes_selected_and_system_derived_dispositions(session, outcome):
    utterance, _item = _base(session)
    now = datetime.now(UTC)
    case = AttentionCase(situation_key="preview-case", title="Fixture", summary="Unrelated text",
                         first_observed_at=now, last_observed_at=now)
    session.add(case)
    session.flush()
    required = CaseItem(
        attention_case_id=case.id, item_key="required", item_type="decision_required",
        resolution_role="required", payload_json={"private": "unrelated"})
    supporting = CaseItem(
        attention_case_id=case.id, item_key="support", item_type="identity_resolution",
        resolution_role="supporting")
    session.add_all([required, supporting])
    session.flush()
    revision = AttentionCaseRevision(
        attention_case_id=case.id, case_ref=case.ref_id, revision=1, title=case.title,
        summary=case.summary, case_item_refs=[required.ref_id, supporting.ref_id],
        admission_rule_ref="fixture", admission_basis_refs=[utterance.ref_id],
        required_case_item_refs=[required.ref_id], canonical_consequence_classes=["decision"],
        content_hash="a" * 64,
    )
    session.add(revision)
    session.flush()
    content = ChangeSetContent.model_validate({
        "basis_refs": [utterance.ref_id], "resolution_changes": [{
            "change_id": "resolve", "mutation_type": "attention_case_resolution",
            "action": "update", "object_type": "attention_case_resolution",
            "object_ref": case.ref_id, "case_revision_ref": revision.ref_id,
            "case_outcome": outcome, "basis_refs": [utterance.ref_id],
            "item_dispositions": [{"case_item_ref": required.ref_id, "disposition": "resolved"}],
        }],
    })
    effect = capture_canonical_patch_preview(session, content)["effects"][0]
    assert effect["available"] is True
    assert effect["before"]["status"] == "open"
    assert effect["after"]["status"] == ("open" if outcome == "keep_open" else outcome)
    assert effect["after"]["item_statuses"][required.ref_id] == "resolved"
    if outcome == "keep_open":
        assert supporting.ref_id not in effect["after"]["item_statuses"]
        assert effect["system_not_pursued_refs"] == []
    else:
        assert effect["after"]["item_statuses"][supporting.ref_id] == "not_pursued"
        assert effect["system_not_pursued_refs"] == [supporting.ref_id]
    assert effect["operator_disposition_refs"] == [required.ref_id]
    assert "Unrelated text" not in json.dumps(effect) and "private" not in json.dumps(effect)
    assert case.status == required.status == supporting.status == "open"
    if outcome == "resolved":
        content.resolution_changes[0].item_dispositions = []
        invalid = capture_canonical_patch_preview(session, content)["effects"][0]
        assert invalid["available"] is False
        assert invalid["reason"] == "required_case_items_unresolved"
    case.latest_revision += 1
    invalid = capture_canonical_patch_preview(session, content)["effects"][0]
    assert invalid["available"] is False
    assert invalid["reason"] == "case_revision_not_applicable"

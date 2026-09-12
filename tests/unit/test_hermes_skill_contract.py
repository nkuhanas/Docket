import re
from pathlib import Path

import yaml

from docket.domain.public_refs import new_public_ref
from docket.schemas.assembly import StageActionUpsert, StageChangesInput

SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-manual-intent/SKILL.md")
TRIAGE_SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-triage/SKILL.md")


def test_manual_skill_uses_ledger_authority_without_redundant_approval() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "current `utt_` reference" in skill
    assert "do not ask for a redundant approval" in skill
    assert "no Record, queue-item, Action, Approval, or compatibility surface" in skill
    assert "`docket_commit_changeset`" in skill
    assert "`docket_resolve_conflict`" in skill


def test_docket_profile_has_no_unbound_generic_clarification_tool() -> None:
    config = Path("hermes/config.example.yaml").read_text(encoding="utf-8")

    assert "    - clarify\n" not in config


def test_manual_skill_preserves_evidence_interpretation_and_conflicts() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Preserve what the Operator said separately from what it means" in skill
    assert "`supersedes`" in skill
    assert "must open or preserve a Conflict" in skill
    assert "do not overwrite canonical state" in skill
    assert "one consolidated clarification" in skill


def test_manual_skill_defines_semantic_readiness_and_staged_protocol() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "every required object resolves to one public ref" in skill
    assert "every event has an exact intended lane" in skill
    assert "Commit readiness is separate" in skill
    assert "not evidence that the Operator's intent became ambiguous" in skill
    assert "not copied into model arguments" in skill
    assert "Confidence, plausibility, or “obvious” is never a substitute" in skill
    for group in (
        "`registry_changes`",
        "`preference_changes`",
        "`lane_changes`",
        "`event_changes`",
        "`tracked_context_changes`",
        "`resolution_changes`",
    ):
        assert group not in skill
    assert "`patch.operations`" in skill
    assert '`operation="action_upsert"`' in skill
    assert "`provider_intents` is deliberately absent" in skill
    assert "`*_change_id` references" in skill
    assert "`add_associated_email_change_id`" in skill
    assert "`add_associated_email_ref`" in skill
    assert "do not preallocate an `idn_`" in skill
    assert "returned current `caserev_`" in skill
    assert "first structurally valid stage, then commit without rejected schema probes" in skill
    assert "commit_mode=" not in skill
    assert "Always stage into Docket's implicit durable draft" in skill
    assert "Review is optional" in skill
    assert "`docket_request_clarification`" in skill
    assert "omitted supporting items deterministically become `not_pursued`" in skill
    assert "`predicate=application_status`" in skill
    assert "Docket deterministically compiles the required Google projection" in skill
    assert 'Never ask the Operator to authorize a later "push to Google"' in skill
    assert "Hermes never formulates, retries, or repairs provider Operations" in skill
    assert "exact discriminated `mutation_types`" in skill
    assert "`item_create`, `task_create`, and `temporal_binding_create`" in skill
    assert "Never describe an unscoped union" in skill
    assert "at most 25 normalized-entry upserts" in skill
    assert "do not fan out into per-event graph or history reads" in skill
    assert "offset-free wall-clock values" in skill
    assert "reads retained PDFs, not image attachments" in skill
    assert "unique `import_entry_id`" in skill
    assert "exact source-fragment locator/hash" in skill
    assert "exact duplicated-title repair rule" in skill
    assert "every other effect fixed" in skill
    assert "Never compress source entries" in skill


def test_sender_example_validates_against_actual_staging_schema() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    examples = re.findall(r"```yaml\n(.*?)\n```", skill, flags=re.DOTALL)
    assert len(examples) == 1
    text = examples[0]
    refs = {name: new_public_ref(prefix) for name, prefix in {
        "utt_CURRENT_MESSAGE": "utt", "idn_EXISTING_SENDER": "idn",
        "pref_EXISTING_POLICY": "pref",
    }.items()}
    for placeholder, ref in refs.items():
        text = text.replace(placeholder, ref)
    args = yaml.safe_load(text)
    assert set(args) == {"assembly_scope", "expected_versions", "patch"}
    # The trusted gateway adds these; they are not part of the model example.
    request = StageChangesInput(
        **args, utterance_ref=refs["utt_CURRENT_MESSAGE"], request_key="fixture:sender",
    )
    operations = request.patch.operations
    assert all(isinstance(operation, StageActionUpsert) for operation in operations)
    actions = [operation.action for operation in operations]
    assert [action.object_type for action in actions] == [
        "identity_handle", "identity_handle", "preference",
    ]
    assert actions[1].payload.add_associated_email_change_id == actions[0].change_id
    assert actions[2].payload.policy_json == {"disposition": "suppress"}
    assert all(action.basis_refs == [refs["utt_CURRENT_MESSAGE"]] for action in actions)
    assert request.assembly_scope.allowed_mutation_types == sorted(
        action.mutation_type for action in actions
    )


def test_manual_skill_keeps_triage_non_authoritative_and_outputs_compact() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Cron evidence and model inference never authorize" in skill
    assert "exact trusted revision binding" in skill
    assert "does not mean the provider call has completed" in skill
    assert "Do not reproduce raw provenance chains" in skill
    assert "Do not tell the Operator to click an approval card" in skill
    assert "merely because the immutable `utt_` exists" in skill
    assert "bounded receipt, effect/provider counts" in skill
    assert "intentionally truncates those samples" in skill


def test_triage_skill_does_not_invent_acknowledgement_work() -> None:
    skill = " ".join(TRIAGE_SKILL_PATH.read_text(encoding="utf-8").split())

    assert "actually asks the operator to reply, submit, pay, acknowledge" in skill
    assert "job-application receipt" in skill
    assert "never an acknowledgement obligation" in skill
    assert "corresponding semantic class" in skill


def test_triage_skill_applies_policy_before_resolution_and_forbids_registration() -> None:
    skill = " ".join(TRIAGE_SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Apply the active structured Preferences" in skill
    assert "Candidate entity refs are suggestions only" in skill
    assert "Never create a lane" in skill
    assert "Organization, Affiliation, Relationship, Fact" in skill
    assert "Every CaseItem must declare `resolution_role`" in skill
    assert "`application_status=submitted`" in skill
    assert "Never extend that Decision by title, name, or semantic similarity" in skill

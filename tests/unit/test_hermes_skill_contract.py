from pathlib import Path

SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-manual-intent/SKILL.md")
TRIAGE_SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-triage/SKILL.md")


def test_manual_skill_uses_ledger_authority_without_redundant_approval() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "current `utt_` reference" in skill
    assert "do not ask for a redundant approval" in skill
    assert "legacy mutation tools are unavailable" in skill
    assert "`docket_commit_changeset`" in skill
    assert "`docket_resolve_conflict`" in skill


def test_manual_skill_preserves_evidence_interpretation_and_conflicts() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Preserve what the Operator said separately from what it means" in skill
    assert "`supersedes`" in skill
    assert "must open or preserve a Conflict" in skill
    assert "do not overwrite canonical state" in skill
    assert "one consolidated clarification" in skill


def test_manual_skill_defines_exact_resolved_intent_and_changeset_groups() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "every required object resolves to one public ref" in skill
    assert "every event has an enabled CalendarLane and a routing Decision" in skill
    assert "Confidence, plausibility, or “obvious” is never a substitute" in skill
    for group in (
        "`registry_changes`",
        "`preference_changes`",
        "`lane_changes`",
        "`event_changes`",
        "`resolution_changes`",
        "`provider_intents`",
    ):
        assert group in skill
    assert "`*_change_id` references" in skill
    assert "`add_associated_email_change_id`" in skill
    assert "`add_associated_email_ref`" in skill
    assert "do not preallocate an `idn_`" in skill


def test_manual_skill_keeps_triage_non_authoritative_and_outputs_compact() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Cron evidence and model inference never authorize" in skill
    assert "exact trusted revision binding" in skill
    assert "does not mean the provider call has completed" in skill
    assert "Do not reproduce raw provenance chains" in skill
    assert "Do not tell the Operator to click an approval card" in skill


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

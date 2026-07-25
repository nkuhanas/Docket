from pathlib import Path

SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-manual-intent/SKILL.md")


def test_manual_intent_skill_requires_button_first_approval_guidance() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "card's **Approve** or **Reject** button" in skill
    assert "operator-runbook-only break-glass mechanism" in skill
    assert "intentionally absent from the model-facing proposal result" in skill


def test_manual_intent_skill_forbids_conflict_data_laundering() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert "Never fetch the canonical record, copy its data" in skill
    assert "merely to manufacture `matched_existing`" in skill


def test_manual_intent_skill_preserves_calendar_freshness_and_explicit_reminders() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Never describe stale or uncovered cache state as current" in skill
    assert "only through the `reminders` discriminator" in skill
    assert "not model-authored text" in skill
    assert "no model-visible direct rule write or disable tool" in skill
    assert "both Google popup and the ISO thread" in skill
    assert "ISO thread" in skill
    assert "never search past sessions for a rule UUID or version" in skill
    assert "`target_scope: event`" in skill
    assert "`target_scope: series`" in skill
    assert "master `recurring_event_id`" in skill
    assert "Never substitute an occurrence ID" in skill


def test_manual_intent_skill_keeps_durable_output_out_of_chat() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "request/response ingress" in skill
    assert "Never duplicate a proposal body" in skill
    assert "do not duplicate that preview in chat" in skill
    assert "Do not start a background terminal process" in skill


def test_manual_intent_skill_uses_independent_course_lifecycles() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "A schedule is not a Docket entity" in skill
    assert "ask one consolidated clarification question" in skill
    assert "Store or explicitly update each course/section as its own canonical record" in skill
    assert "one conflict or failure does not roll back successful siblings" in skill
    assert "`docket_propose_course_reconciliation` in `sync` mode" in skill
    assert "Omitting a previously stored course from a later import has no effect" in skill
    assert "`docket_propose_course_reconciliation` in `drop` mode" in skill
    assert "partial provider success leaves the course active for retry" in skill
    assert "`docket_restore_record`" in skill
    assert "legacy compatibility tools" in skill
    assert "Do not call update merely to restate equal data" in skill
    assert "version-preserving no-op" in skill
    assert "Under `off`, never propose" in skill
    assert "Under `explicit_only`, propose only" in skill
    assert "Cancellation is always explicit" in skill
    assert "meeting values take precedence over the associated term" in skill
    assert "leave it null so Docket can derive the corresponding term default" in skill
    assert "Never replace a shorter supplied course range" in skill
    assert "Docket-owned daily threads under `#docket-queue`" in skill
    assert "queue root and `#docket-system` remain non-conversational" in skill

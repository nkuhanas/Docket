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


def test_manual_intent_skill_keeps_durable_output_out_of_chat() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "request/response ingress" in skill
    assert "Never duplicate a proposal body" in skill
    assert "do not duplicate that preview in chat" in skill
    assert "Do not start a background terminal process" in skill


def test_manual_intent_skill_uses_one_atomic_schedule_flow() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Call `docket_store_term_schedule` exactly once" in skill
    assert "Never loop over `docket_store_record` per term or course" in skill
    assert "call `docket_propose_term_schedule` exactly once" in skill
    assert "do not wait for a second “propose it” prompt" in skill
    assert "ask one consolidated clarification question" in skill
    assert "one aggregate proposal" in skill
    assert "Under `off`, never propose" in skill
    assert "Under `explicit_only`, propose only" in skill
    assert "Cancellation is always explicit" in skill

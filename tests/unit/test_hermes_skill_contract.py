from pathlib import Path

SKILL_PATH = Path(
    "hermes/plugin/docket_discord/skills/docket-manual-intent/SKILL.md"
)


def test_manual_intent_skill_requires_button_first_approval_guidance() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

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
    assert "only when the user explicitly asks for a standing" in skill
    assert "not model-authored text" in skill
    assert "reminder_destination_not_allowed" in skill
    assert "never search past sessions for a rule UUID or version" in skill

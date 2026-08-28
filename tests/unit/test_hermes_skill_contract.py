from pathlib import Path

SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-manual-intent/SKILL.md")
TRIAGE_SKILL_PATH = Path("hermes/plugin/docket_discord/skills/docket-triage/SKILL.md")


def test_manual_intent_skill_requires_authority_aware_execution_guidance() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "execute directly" in skill
    assert "queues execution without an approval card" in skill
    assert "A conflict may instead return a resolution card" in skill
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
    assert "only when `docket_queue` is enabled" in skill
    assert "ISO thread" in skill
    assert "never search past sessions for a rule UUID or version" in skill
    assert "`target_scope: event`" in skill
    assert "`target_scope: series`" in skill
    assert "master `recurring_event_id`" in skill
    assert "Never substitute an occurrence ID" in skill
    assert "omit it from the timing payload" in skill
    assert "configured `DOCKET_TIMEZONE`" in skill
    assert "do not ask for a timezone merely to restate that default" in skill


def test_manual_intent_skill_keeps_durable_output_out_of_chat() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "request/response ingress" in skill
    assert "Never duplicate a proposal body" in skill
    assert "Never duplicate a proposal body" in skill
    assert "Do not start a background terminal process" in skill


def test_manual_intent_skill_uses_independent_course_lifecycles() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "A schedule is not a Docket entity" in skill
    assert "ask one consolidated clarification question" in skill
    assert "Store or explicitly update each course/section as its own canonical record" in skill
    assert "one conflict or failure does not roll back successful siblings" in skill
    assert "`docket_apply_course_intent` in `sync` mode" in skill
    assert "Omitting a previously stored course from a later import has no effect" in skill
    assert "`docket_apply_course_intent` in `drop` mode" in skill
    assert "partial provider success leaves the course active for retry" in skill
    assert "`docket_restore_record`" in skill
    assert "docket_store_term_schedule" not in skill
    assert "docket_propose_term_schedule" not in skill
    assert "Do not call update merely to restate equal data" in skill
    assert "version-preserving no-op" in skill
    assert "current message explicitly requests Calendar application" in skill
    assert "proposal mode governs inferred suggestions" in skill
    assert "Drop only from an explicit current operator request" in skill
    assert "meeting values take precedence over the associated term" in skill
    assert "leave it null so Docket can derive the corresponding term default" in skill
    assert "Never replace a shorter supplied course range" in skill
    assert "Docket-owned daily threads under `#docket-queue`" in skill
    assert "queue root and `#docket-system` remain non-conversational" in skill


def test_manual_intent_skill_uses_entity_registry_before_guessing() -> None:
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "use `docket_search_entities`" in skill
    assert "use `is_operator: true`" in skill
    assert "subject predicate object" in skill
    assert "Use `docket_get_entity` immediately before relying" in skill
    assert "No search result is not permission to invent a fact" in skill
    assert "never populate a seed list or create inferred social relationships" in skill
    assert "there may be only one active operator identity" in skill
    assert "never reconstruct the whole profile" in skill
    assert "`docket_update_entity_relation`" in skill
    assert "`docket_retract_entity_relation`" in skill


def test_triage_skill_does_not_invent_acknowledgement_work() -> None:
    skill = " ".join(TRIAGE_SKILL_PATH.read_text(encoding="utf-8").split())

    assert "actually asks the operator to reply, submit, pay, acknowledge" in skill
    assert "job-application receipt" in skill
    assert "never an acknowledgement obligation" in skill
    assert "corresponding typed candidate" in skill


def test_triage_skill_ranks_relevance_before_entities_and_bundles_registration() -> None:
    skill = " ".join(TRIAGE_SKILL_PATH.read_text(encoding="utf-8").split())
    manual = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Rank calendar relevance **before** requesting any entity resolution" in skill
    assert "Every `event` candidate must assign one explicit `calendar_relevance`" in skill
    assert "bundles its registration into the event proposal" in skill
    assert "/opt/data/preferences/AGENT.md" in manual
    assert "/opt/data/preferences/TRIAGE.md" in manual
    assert "I don't want to go to football games this semester" in manual
    assert "ignore that item" in manual

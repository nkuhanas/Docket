from pathlib import Path

import pytest
import yaml

READINESS = Path("deltas/docket-tracked-context-readiness-status-08-29-2026.yaml")
NAMESPACE = Path("deltas/docket-tracked-context-namespace-cutover-08-29-2026.yaml")
TOOLS = Path("deltas/docket-tracked-context-tool-cutover-matrix-08-29-2026.yaml")
TRIAGE = Path("deltas/docket-tracked-context-triage-admission-matrix-08-29-2026.yaml")


def _load(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_bytes())


@pytest.mark.integration
def test_tracked_context_namespace_has_one_clean_meaning_per_prefix() -> None:
    namespace = _load(NAMESPACE)
    prefixes = namespace["prefixes"]
    assert isinstance(prefixes, dict)
    assert len(prefixes) == 44
    assert len(prefixes) == len(set(prefixes))
    assert len(prefixes.values()) == len(set(prefixes.values()))
    assert prefixes["item"] == "Item"
    assert prefixes["citem"] == "CaseItem"
    assert prefixes["bentry"] == "BriefEntry"
    assert prefixes["src"] == "Source"
    assert prefixes["acct"] == "ProviderAccount"
    assert prefixes["conf"] == "Conflict"
    assert prefixes["sattempt"] == "SemanticRequestAttempt"
    assert prefixes["proj"] == "OperatorProjection"
    assert prefixes["trace"] == "ConversationalToolTrace"
    assert set(namespace["retired_prefixes"]) == {
        "itm",
        "dproj",
        "prompt",
        "cnf",
        "satt",
        "lease",
    }
    validation = namespace["validation"]
    assert validation["runtime_aliases_permitted"] is False
    assert validation["old_bare_ulid_fallback_permitted"] is False


@pytest.mark.integration
def test_every_current_tool_has_one_clean_cutover_disposition() -> None:
    matrix = _load(TOOLS)
    interactive = matrix["interactive"]
    entries = interactive["entries"]
    current = [entry["current"] for entry in entries if entry["current"] is not None]
    target = [entry["target"] for entry in entries if entry["target"] is not None]
    assert len(current) == interactive["current_count"] == 22
    assert len(current) == len(set(current))
    assert len(target) == interactive["target_count"] == 19
    assert len(target) == len(set(target))
    assert sum(entry["disposition"] == "remove" for entry in entries) == 5
    assert sum(entry["disposition"] == "add" for entry in entries) == 2
    assert "docket_query_items" in target
    assert "docket_get_item_context" in target
    assert "docket_get_record" not in target
    assert "docket_get_queue_item" not in target
    assert "docket_search_records" not in target

    triage = matrix["triage"]
    triage_current = [entry["current"] for entry in triage["entries"]]
    triage_target = [entry["target"] for entry in triage["entries"]]
    assert len(triage_current) == triage["current_count"] == 4
    assert len(set(triage_current)) == 4
    assert len(set(triage_target)) == triage["target_count"] == 4
    assert "docket_get_attention_case" in triage_target
    assert matrix["constraints"]["old_name_aliases_permitted"] is False
    assert matrix["constraints"]["triage_mutation_tools"] == []


@pytest.mark.integration
def test_triage_admission_fixture_matrix_covers_every_disposition_boundary() -> None:
    matrix = _load(TRIAGE)
    fixtures = matrix["fixtures"]
    dispositions = {
        fixture.get("disposition", fixture.get("submitted_disposition"))
        for fixture in fixtures
    }
    assert dispositions == {"suppress", "brief", "attention_case"}
    attention = [
        fixture
        for fixture in fixtures
        if fixture.get("disposition") == "attention_case"
        and "operator_resolution" not in fixture
    ]
    assert attention
    assert all(fixture["required_case_items"] for fixture in attention)
    identity_only = next(
        fixture for fixture in fixtures if fixture["fixture"] == "unknown_identity_only"
    )
    assert identity_only["disposition"] == "brief"
    invalid = next(
        fixture
        for fixture in fixtures
        if fixture["fixture"] == "invalid_identity_only_attention_submission"
    )
    assert invalid["service_result"] == "rejected_validation"
    assert invalid["persisted_case_items"] == 0
    assert matrix["coverage"]["triage_canonical_mutation"] is False


@pytest.mark.integration
def test_readiness_status_is_honest_about_remaining_start_blockers() -> None:
    readiness = _load(READINESS)
    assert readiness["frozen_artifact_hash"] == (
        "830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48"
    )
    assert readiness["authority"]["production_reset_authority"] is False
    gate = readiness["implementation_gate"]
    assert gate["total"] == 8
    assert gate["resolved"] == 3
    assert gate["implementation_start_permitted"] is False
    blockers = readiness["implementation_start_blockers"]
    assert len(blockers) == 8
    assert {blocker["blocker_ref"] for blocker in blockers} == {
        "governance_provenance_closure_export_and_restore_rehearsal",
        "provider_effect_and_binding_disposition_inventory",
        "namespace_cutover_collision_scan",
        "tracked_context_requirement_traceability",
        "clean_tool_contract_cutover_mapping",
        "clean_reset_cutover_rehearsal",
        "attachment_retention_and_restore_verification",
        "triage_admission_rule_and_fixture_matrix",
    }
    assert readiness["production_actions"] == {
        "reset_executed": False,
        "production_data_deleted": False,
        "provider_mutation_performed": False,
        "deployment_performed": False,
    }

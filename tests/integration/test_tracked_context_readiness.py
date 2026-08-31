import csv
from pathlib import Path

import pytest
import yaml

READINESS = Path("deltas/docket-tracked-context-readiness-status-08-29-2026.yaml")
NAMESPACE = Path("deltas/docket-tracked-context-namespace-cutover-08-29-2026.yaml")
TOOLS = Path("deltas/docket-tracked-context-tool-cutover-matrix-08-29-2026.yaml")
TRIAGE = Path("deltas/docket-tracked-context-triage-admission-matrix-08-29-2026.yaml")
TRACEABILITY = Path("deltas/docket-tracked-context-traceability-08-29-2026.csv")
REHEARSAL_EVIDENCE = Path(
    "deltas/docket-tracked-context-rehearsal-evidence-08-29-2026.yaml"
)

NORMATIVE_REFS = {
    "ONT-TRACK-INV-0010",
    "ONT-TRACK-DEC-0003",
    "ONT-TRACK-REQ-0042",
    "ONT-TRACK-INV-0001",
    "ONT-TRACK-INV-0011",
    "ONT-TRACK-INV-0012",
    "ONT-TRACK-REQ-0043",
    "ONT-TRACK-INV-0002",
    "ONT-TRACK-REQ-0001",
    "ONT-TRACK-INV-0003",
    "ONT-TRACK-REQ-0002",
    "ONT-TRACK-REQ-0003",
    "ONT-TRACK-REQ-0004",
    "ONT-TRACK-REQ-0005",
    "ONT-TRACK-REQ-0006",
    "ONT-TRACK-REQ-0038",
    "ONT-TRACK-REQ-0007",
    "ONT-TRACK-DEC-0001",
    "ONT-TRACK-REQ-0008",
    "ONT-TRACK-INV-0004",
    "ONT-TRACK-REQ-0009",
    "ONT-TRACK-REQ-0010",
    "ONT-TRACK-INV-0005",
    "ONT-TRACK-REQ-0011",
    "ONT-TRACK-REQ-0012",
    "ONT-TRACK-REQ-0013",
    "ONT-TRACK-INV-0006",
    "ONT-TRACK-REQ-0014",
    "ONT-TRACK-REQ-0015",
    "ONT-TRACK-REQ-0016",
    "ONT-TRACK-REQ-0017",
    "ONT-TRACK-REQ-0039",
    "ONT-TRACK-INV-0007",
    "ONT-TRACK-REQ-0018",
    "ONT-TRACK-REQ-0040",
    "ONT-TRACK-REQ-0019",
    "ONT-TRACK-INV-0013",
    "ONT-TRACK-REQ-0044",
    "ONT-TRACK-REQ-0045",
    "ONT-TRACK-REQ-0046",
    "ONT-TRACK-REQ-0047",
    "ONT-TRACK-REQ-0048",
    "ONT-TRACK-INV-0008",
    "ONT-TRACK-REQ-0020",
    "ONT-TRACK-REQ-0021",
    "ONT-TRACK-REQ-0022",
    "ONT-TRACK-REQ-0023",
    "ONT-TRACK-REQ-0024",
    "ONT-TRACK-REQ-0041",
    "ONT-TRACK-REQ-0025",
    "ONT-TRACK-DEC-0002",
    "ONT-TRACK-REQ-0026",
    "ONT-TRACK-REQ-0027",
    "ONT-TRACK-REQ-0028",
    "ONT-TRACK-REQ-0029",
    "ONT-TRACK-REQ-0030",
    "ONT-TRACK-REQ-0031",
    "ONT-TRACK-REQ-0032",
    "ONT-TRACK-REQ-0033",
    "ONT-TRACK-REQ-0049",
    "ONT-TRACK-REQ-0034",
    "ONT-TRACK-REQ-0035",
    "ONT-TRACK-REQ-0036",
    "ONT-TRACK-REQ-0037",
    "ONT-TRACK-REQ-0050",
    "ONT-TRACK-REQ-0051",
    "ONT-TRACK-INV-0014",
    "ONT-TRACK-INV-0009",
}


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
    assert len(target) == interactive["target_count"] == 20
    assert len(target) == len(set(target))
    assert sum(entry["disposition"] == "remove" for entry in entries) == 5
    assert sum(entry["disposition"] == "add" for entry in entries) == 3
    assert "docket_query_items" in target
    assert "docket_get_item_context" in target
    assert "docket_read_attachment_text" in target
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
def test_every_normative_tracked_context_clause_is_mapped() -> None:
    with TRACEABILITY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(NORMATIVE_REFS) == 68
    assert {row["requirement_ref"] for row in rows} == NORMATIVE_REFS
    assert len({row["requirement_ref"] for row in rows}) == len(rows)
    assert all(row["status"] == "implemented_verified" for row in rows)
    assert all(row["test_refs"] or row["operational_verification_refs"] for row in rows)
    assert all(row["acceptance_refs"] for row in rows)

    for row in rows:
        for test_ref in row["test_refs"].split("|"):
            path_value, separator, function_name = test_ref.partition("::")
            assert separator and function_name.startswith("test_")
            source = Path(path_value).read_text(encoding="utf-8")
            assert f"def {function_name}(" in source
        for service_ref in row["service_refs"].split("|"):
            if service_ref.startswith(("src/", "migrations/")):
                assert Path(service_ref).is_file()


@pytest.mark.integration
def test_readiness_status_closes_every_start_blocker_without_reset_authority() -> None:
    readiness = _load(READINESS)
    assert readiness["frozen_artifact_hash"] == (
        "830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48"
    )
    assert readiness["authority"]["production_reset_authority"] is False
    gate = readiness["implementation_gate"]
    assert gate["total"] == 8
    assert gate["resolved"] == 8
    assert gate["in_progress"] == 0
    assert gate["blocked_pending_implementation"] == 0
    assert gate["implementation_start_permitted"] is True
    blockers = readiness["implementation_start_blockers"]
    assert len(blockers) == 8
    assert all(blocker["status"] == "resolved" for blocker in blockers)
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


@pytest.mark.integration
def test_rehearsal_evidence_is_hash_bound_and_nonmutating() -> None:
    evidence = _load(REHEARSAL_EVIDENCE)
    assert evidence["rehearsal_revision"] == (
        "18356da68dd76a14cedf675a71a35704d6121d00"
    )
    assert evidence["governance_closure"] == {
        "file": "governance-closure.json",
        "closure_sha256": (
            "2d59186e1cdeca00e1f8a338e15fa01e35647fd95c32c44fc79a1043b167d6bf"
        ),
        "rows": 72,
        "unresolved_authority_refs": 0,
        "unresolved_non_authority_refs": 0,
        "verified_specification_signoffs": 4,
        "exact_public_refs_and_row_hashes_exported": True,
        "isolated_clean_schema_restore_verified": True,
    }
    provider = evidence["provider_disposition"]
    assert provider["targets"] == {
        "google_calendars": 6,
        "google_calendar_events": 30,
        "total": 36,
    }
    assert provider["dispositions"] == {
        "leave_external_unmanaged": 36,
        "adopt_into_clean_model": 0,
        "delete_by_separately_authorized_provider_operation": 0,
    }
    assert provider["running_or_uncertain_blockers"] == 0
    assert evidence["attachment_restore"]["post_restore_decryption_verified"] is True
    assert evidence["authority_boundary"] == {
        "implementation_readiness_only": True,
        "production_reset_authority": False,
        "production_reset_executed": False,
        "production_data_deleted": False,
        "provider_mutation_authorized": False,
        "provider_mutation_performed": False,
        "deployment_performed": False,
    }

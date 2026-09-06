from __future__ import annotations

import csv
import re
from pathlib import Path

import yaml


READINESS = Path("deltas/docket-incremental-changeset-readiness-09-06-2026.yaml")
TRACEABILITY = Path("deltas/docket-incremental-changeset-traceability-09-06-2026.csv")
SPEC = Path("deltas/docket-incremental-changeset-assembly-delta-09-06-2026.md")


def _readiness() -> dict[str, object]:
    loaded = yaml.safe_load(READINESS.read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def test_readiness_records_exact_signed_authority_without_deployment_authority() -> None:
    readiness = _readiness()
    assert readiness["document_ref"] == (
        "ONT-DELTA-2026-09-06-INCREMENTAL-CHANGESET-ASSEMBLY"
    )
    assert readiness["frozen_artifact_hash"] == (
        "0557d095d8c4d166f4f3f8a47d58247842649bbfbfc4f696223e065754d858d7"
    )
    authority = readiness["authority"]
    assert authority == {
        "architecture_authority": True,
        "implementation_authority": "amendment_scope",
        "authorized_scope": (
            "incremental_changeset_assembly_concurrency_idempotency_and_compilation"
        ),
        "production_reset_authority": False,
        "deployment_authority": False,
        "signoff_evidence": {
            "operator_utterance_ref": "utt_01M1W7634P9N080KCGBRNQN53R",
            "decision_ref": "dec_01M1W7648P7YJZ22GRD114WSBV",
            "audit_ref": "aud_01M1W7648QWKN6Q4989Q06B7TM",
            "agent_response_ref": "rsp_01M1W76495SA0FS5AEWWPCYJKC",
            "response_delivery_state": "delivered",
        },
        "prerequisite_decision_refs": [
            "dec_01M13MANM19BX22EW8QC8AH9DT",
            "dec_01M1587SE1JX3BVQ1QZBQKX6T7",
            "dec_01M15EHKNXVKRBM7MZ3FN39X3E",
            "dec_01M18DYEYJVVJ7TW5VQQBCA6NC",
        ],
    }


def test_all_eight_preimplementation_design_gates_are_exact() -> None:
    readiness = _readiness()
    gate = readiness["implementation_start_gate"]
    assert gate == {
        "total": 8,
        "design_complete": 8,
        "blocked": 0,
        "implementation_start_permitted": True,
        "implementation_complete": False,
        "deployment_permitted": False,
    }
    assert readiness["tool_migration_matrix"]["target_interactive_count"] == 22
    assert readiness["tool_migration_matrix"]["target_triage_count"] == 4
    assert readiness["persistence_and_concurrency_design"]["automatic_merge"] is False
    assert readiness["typed_stage_schema"]["patch"]["max_operations_per_call"] == 50
    assert readiness["compiler_boundary_matrix"]["compiler_version"] == 1
    assert len(readiness["idempotency_and_ingress_design"]["named_fixtures"]) == 7
    assert readiness["output_and_workload_contract"]["output_budgets"] == {
        "ordinary_serialized_json_bytes": 16384,
        "audit_serialized_json_bytes": 65536,
        "review_default_items": 25,
        "review_max_items": 100,
    }
    assert readiness["hermes_contract_plan"]["profile_parity"] == {
        "interactive_tools": 22,
        "triage_tools": 4,
    }


def test_traceability_plans_every_normative_clause_and_acceptance() -> None:
    clause_ids = set(
        re.findall(
            r"\*\*(ONT-ASSEMBLY-(?:INV|DEF|REQ|ACC)-\d{4})\b",
            SPEC.read_text(encoding="utf-8"),
        )
    )
    with TRACEABILITY.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(clause_ids) == 44
    assert {row["clause_id"] for row in rows} == clause_ids
    assert all(row["status"] == "planned" for row in rows)
    assert sum(row["kind"] == "acceptance" for row in rows) == 16
    assert all(row["implementation_targets"] for row in rows)
    assert all("::test_" in row["test_plan"] for row in rows)


def test_readiness_separates_test_plans_from_predeployment_results() -> None:
    readiness = _readiness()
    assert readiness["status"] == "pre_implementation_design_complete"
    assert readiness["traceability_plan"]["preimplementation_status"] == "planned"
    assert readiness["traceability_plan"]["required_predeployment_status"] == (
        "implemented_and_verified"
    )
    gate = readiness["predeployment_gate"]
    assert gate["implementation_dependent_results_required"] is True
    assert gate["deployment_requires_separate_operator_direction"] is True
    assert gate["live_provider_check"] == {
        "operator_present": True,
        "simulated_in_ci": False,
    }

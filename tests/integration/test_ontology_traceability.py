import ast
import csv
import hashlib
import re
from pathlib import Path

import pytest
import yaml

TRACEABILITY = Path("deltas/docket-ontology-traceability-08-27-2026.csv")
ROLLOUT_EVIDENCE = Path("docs/ontology-rollout-verification.md")
FROZEN_SPEC = Path("deltas/docket-ontology-delta-08-27-2026.md")
READINESS_SPEC = Path("deltas/docket-ontology-acceptance-readiness-08-27-2026.md")
READINESS_STATUS = Path("deltas/docket-ontology-readiness-status-08-27-2026.yaml")


def _refs(value: str) -> list[str]:
    return [item for item in value.split("|") if item]


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


@pytest.mark.integration
def test_every_normative_requirement_is_implemented_and_verification_mapped() -> None:
    with TRACEABILITY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 112
    assert len({row["requirement_ref"] for row in rows}) == 112
    assert [row["requirement_ref"] for row in rows] == [
        f"ONT-REQ-{index:04d}" for index in range(1, 113)
    ]
    for row in rows:
        evidence_refs = [
            *_refs(row["test_refs"]),
            *_refs(row["operational_verification_refs"]),
        ]
        assert evidence_refs, row["requirement_ref"]
        for node_ref in _refs(row["test_refs"]):
            path_value, separator, function_name = node_ref.partition("::")
            assert separator and function_name.startswith("test_"), node_ref
            path = Path(path_value)
            assert path.is_file(), node_ref
            assert function_name in _test_functions(path), node_ref
        for path_field in ("migration_refs", "service_refs"):
            for path_value in _refs(row[path_field]):
                assert Path(path_value).exists(), (row["requirement_ref"], path_value)
    assert all(row["status"] == "implemented_verified" for row in rows)
    assert ROLLOUT_EVIDENCE.is_file()

    readiness = yaml.safe_load(READINESS_STATUS.read_bytes())
    assert readiness["frozen_artifact_hash"] == (
        "3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1"
    )
    traceability = next(
        item
        for item in readiness["implementation_start_blockers"]
        if item["blocker_ref"] == "ONT-OPEN-0005"
    )["evidence"]["traceability"]
    assert traceability["path"] == str(TRACEABILITY)
    assert hashlib.sha256(TRACEABILITY.read_bytes()).hexdigest() == traceability["sha256"]

    # Private source handoffs are retained outside Git. Operator checkouts
    # validate their exact contents; clean GitHub checkouts validate the
    # checked-in signed readiness record, traceability rows, and concrete test
    # targets without requiring private provenance to be published.
    private_sources_available = FROZEN_SPEC.is_file() and READINESS_SPEC.is_file()
    if private_sources_available:
        assert hashlib.sha256(FROZEN_SPEC.read_bytes()).hexdigest() == (
            readiness["frozen_artifact_hash"]
        )
        acceptance = next(
            item
            for item in readiness["implementation_start_blockers"]
            if item["blocker_ref"] == "ONT-OPEN-0005"
        )["evidence"]["acceptance_addendum"]
        assert hashlib.sha256(READINESS_SPEC.read_bytes()).hexdigest() == acceptance[
            "sha256"
        ]
        normative_sources = "\n".join(
            (
                FROZEN_SPEC.read_text(encoding="utf-8"),
                READINESS_SPEC.read_text(encoding="utf-8"),
            )
        )

    operational_source = ROLLOUT_EVIDENCE.read_text(encoding="utf-8")
    for row in rows:
        for ref_field in ("decision_refs", "tool_contract_refs", "acceptance_refs"):
            for ref in _refs(row[ref_field]):
                if ref.startswith("ONT-"):
                    assert re.fullmatch(r"ONT-(?:DEC|TOOL|ACC)-\d{4}", ref), (
                        row["requirement_ref"],
                        ref,
                    )
                    if private_sources_available:
                        assert ref in normative_sources, (row["requirement_ref"], ref)
        for ref in _refs(row["operational_verification_refs"]):
            assert ref in operational_source, (row["requirement_ref"], ref)


@pytest.mark.integration
def test_externally_visible_requirements_have_acceptance_coverage() -> None:
    with TRACEABILITY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    externally_visible = [
        row
        for row in rows
        if row["tool_contract_refs"]
        or row["acceptance_refs"]
        or any(
            token in row["service_refs"]
            for token in ("hermes", "discord", "brief", "tool", "mcp")
        )
    ]
    assert externally_visible
    assert all(row["acceptance_refs"] for row in externally_visible)

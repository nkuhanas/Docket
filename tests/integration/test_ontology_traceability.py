import ast
import csv
from pathlib import Path

import pytest

TRACEABILITY = Path("deltas/docket-ontology-traceability-08-27-2026.csv")
ROLLOUT_EVIDENCE = Path("docs/ontology-rollout-verification.md")
FROZEN_SPEC = Path("deltas/docket-ontology-delta-08-27-2026.md")
READINESS_SPEC = Path("deltas/docket-ontology-acceptance-readiness-08-27-2026.md")


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

    normative_sources = "\n".join(
        (
            FROZEN_SPEC.read_text(encoding="utf-8"),
            READINESS_SPEC.read_text(encoding="utf-8"),
        )
    )
    operational_source = "\n".join(
        (
            ROLLOUT_EVIDENCE.read_text(encoding="utf-8"),
            READINESS_SPEC.read_text(encoding="utf-8"),
        )
    )
    for row in rows:
        for ref_field in ("decision_refs", "tool_contract_refs", "acceptance_refs"):
            for ref in _refs(row[ref_field]):
                if ref.startswith("ONT-"):
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

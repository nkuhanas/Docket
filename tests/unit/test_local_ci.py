import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("local_ci", "scripts/local_ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def test_policy_paths_exist_and_native_does_not_select_retired_gateway():
    groups = json.loads(Path("tests/ci-policy.json").read_text())["groups"]
    assert all(Path(path).is_file() for paths in groups.values() for path in paths)
    assert all("hermes" not in path and "actor_gate" not in path for path in groups["native"])


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["docs/operations-runbook.md"], ["native"]),
        (["src/docket/providers/calendar.py"], ["native", "provider"]),
        (["migrations/clean_versions/new.py"], ["native", "storage"]),
        (["scripts/docket"], ["native", "release"]),
        (["uv.lock"], ["native", "provider", "release", "storage"]),
    ],
)
def test_changed_area_selection(paths, expected):
    assert ci.groups_for(paths) == expected
    assert ci.groups_for([], same_tree=True) == ["native", "provider", "release", "storage"]


def valid_receipt():
    current = {"tree": "tree", "lock_sha256": "lock", "policy_sha256": "policy"}
    return current, {
        "schema": 1,
        "identity": current.copy(),
        "image": "sha256:image",
        "checks": dict.fromkeys(["native", "static", "postgres"], "passed"),
    }


@pytest.mark.parametrize(
    "fault", ["source", "lock", "policy", "missing", "failed", "image", "label"]
)
def test_stale_or_incomplete_evidence_rejects(fault):
    current, receipt = valid_receipt()
    image, tree = "sha256:image", "tree"
    if fault in {"source", "lock", "policy"}:
        key = {"source": "tree", "lock": "lock_sha256", "policy": "policy_sha256"}[fault]
        current[key] = "different"
    elif fault == "missing":
        receipt["checks"].pop("postgres")
    elif fault == "failed":
        receipt["checks"]["native"] = "failed"
    elif fault == "image":
        image = "sha256:other"
    else:
        tree = "other"
    with pytest.raises(ValueError):
        ci.verify_receipt(receipt, current, ["native"], image, tree)


def test_receipt_accepts_same_tree_with_different_commit():
    current, receipt = valid_receipt()
    receipt["revision"] = "original-commit"
    ci.verify_receipt(receipt, current, ["native"], "sha256:image", "tree")
    with pytest.raises(ValueError, match="required"):
        ci.verify_receipt(receipt, current, ["native", "storage"], "sha256:image", "tree")


def test_snapshot_excludes_ignored_runtime_and_private_sources(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("private/\n.runtime/\n")
    (tmp_path / "tracked").write_text("source")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret").write_text("synthetic secret")
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime/state").write_text("runtime")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=tmp_path, text=True).strip()
    listing = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", tree], cwd=tmp_path, text=True
    ).splitlines()
    assert listing == [".gitignore", "tracked"]

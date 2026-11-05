"""Local checks and content-bound release evidence; no production credentials."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = Path("tests/ci-policy.json")


def run(
    args: list[str], *, cwd: Path = ROOT, capture: bool = False, env: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE if capture else None, text=True
    )
    return result.stdout.strip() if capture else ""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(root: Path = ROOT) -> dict[str, str]:
    tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, capture=True)
    listing = subprocess.check_output(["git", "ls-tree", "-rz", tree], cwd=root)
    return {
        "tree": tree,
        "source_sha256": hashlib.sha256(listing).hexdigest(),
        "lock_sha256": digest(root / "uv.lock"),
        "policy_sha256": digest(root / POLICY),
        "checker_sha256": digest(root / "scripts/local_ci.py"),
    }


def require_clean(root: Path = ROOT) -> None:
    if run(["git", "status", "--porcelain"], cwd=root, capture=True):
        raise ValueError("release validation requires clean committed source")


def groups_for(paths: list[str], *, same_tree: bool = False) -> list[str]:
    groups = {"native"}
    if same_tree:
        return ["native", "provider", "release", "storage"]
    for path in paths:
        if path.startswith(("src/docket/providers/", "src/docket/worker/")):
            groups.add("provider")
        if path.startswith(
            ("src/docket/models/", "migrations/", "src/docket/services/", "src/docket/schemas/")
        ):
            groups.add("storage")
        if path.startswith(("scripts/", "tests/", ".github/")) or path in {
            "Dockerfile",
            "docker-compose.yml",
            "pyproject.toml",
            "uv.lock",
            ".dockerignore",
        }:
            groups.add("release")
        if path in {"pyproject.toml", "uv.lock"}:
            groups.update({"provider", "storage"})
    return sorted(groups)


def changed_paths(root: Path, base: str, tree: str) -> list[str]:
    output = run(["git", "diff", "--name-only", base, tree], cwd=root, capture=True)
    return output.splitlines()


def check(groups: list[str], root: Path = ROOT) -> None:
    policy = json.loads((root / POLICY).read_text())
    if groups == ["all"]:
        paths = ["tests"]
    else:
        paths = sorted({path for group in groups for path in policy["groups"][group]})
    run(["uv", "sync", "--frozen"], cwd=root)
    run(["uv", "run", "pytest", *paths], cwd=root)
    run(["uv", "run", "ruff", "check", "."], cwd=root)
    run(["uv", "run", "mypy"], cwd=root)


def docker() -> list[str]:
    for command in (["docker"], ["sudo", "-n", "docker"]):
        try:
            subprocess.run(
                [*command, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            return command
        except (OSError, subprocess.CalledProcessError):
            continue
    raise ValueError("Docker unavailable for release validation")


def verify_receipt(
    receipt: dict, current: dict[str, str], required: list[str], image_id: str, image_tree: str
) -> None:
    if receipt.get("schema") != 1 or receipt.get("identity") != current:
        raise ValueError("local validation is missing or stale for this source/policy/lockfile")
    outcomes = receipt.get("checks", {})
    if any(outcomes.get(group) != "passed" for group in [*required, "static", "postgres"]):
        raise ValueError("local validation is missing required successful checks")
    if image_id != receipt.get("image") or image_tree != current["tree"]:
        raise ValueError("validated image is missing or belongs to different source")


def receipt_path(current: dict[str, str], root: Path = ROOT) -> Path:
    return root / ".runtime/validation" / (current["tree"] + ".json")


def verify(root: Path = ROOT) -> dict:
    require_clean(root)
    current = identity(root)
    try:
        receipt = json.loads(receipt_path(current, root).read_text())
        base = receipt["base_tree"]
        required = groups_for(
            changed_paths(root, base, current["tree"]), same_tree=base == current["tree"]
        )
        image = receipt["image"]
        details = json.loads(run([*docker(), "image", "inspect", image], capture=True))[0]
        verify_receipt(
            receipt,
            current,
            required,
            details["Id"],
            details["Config"]["Labels"].get("io.docket.source-tree", ""),
        )
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "local release evidence unavailable; run scripts/docket validate-release"
        ) from exc
    return receipt


def validate(root: Path = ROOT) -> None:
    require_clean(root)
    current = identity(root)
    target = receipt_path(current, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            previous = verify(root)
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        else:
            print(json.dumps({"reused_tree": current["tree"], "image": previous["image"]}))
            return
    target.unlink(missing_ok=True)
    revision = run(["git", "rev-parse", "HEAD"], cwd=root, capture=True)
    base_commit = run(["git", "merge-base", "HEAD", "origin/main"], cwd=root, capture=True)
    base = run(["git", "rev-parse", base_commit + "^{tree}"], cwd=root, capture=True)
    groups = groups_for(
        changed_paths(root, base, current["tree"]), same_tree=base == current["tree"]
    )
    started = time.monotonic()
    archive = subprocess.check_output(["git", "archive", current["tree"]], cwd=root)
    engine = docker()
    image_tag = "docket-verified:" + current["tree"]
    with tempfile.TemporaryDirectory(prefix="docket-validation-") as temporary:
        snapshot = Path(temporary)
        # The archive contains only tracked source and synthetic fixtures. The
        # non-root smoke container must traverse its read-only source mount.
        snapshot.chmod(0o755)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(snapshot, filter="data")
        run(["bash", "scripts/docket", "_check-groups", *groups], cwd=snapshot)
        run(
            [
                *engine,
                "build",
                "--build-arg",
                "DOCKET_BUILD_REVISION=" + revision,
                "--label",
                "io.docket.source-tree=" + current["tree"],
                "-t",
                image_tag,
                ".",
            ],
            cwd=snapshot,
        )
        image = run([*engine, "image", "inspect", image_tag, "--format", "{{.Id}}"], capture=True)
        environment = {
            **os.environ,
            "DOCKET_IMAGE": image,
            "DOCKET_SMOKE_PROJECT": f"docket-validation-{current['tree'][:12]}-{os.getpid()}",
        }
        run(
            [
                "bash",
                "scripts/docket",
                "compose-smoke",
                *(["--extended"] if "storage" in groups else []),
            ],
            cwd=snapshot,
            env=environment,
        )
    require_clean(root)
    if identity(root) != current:
        raise ValueError("source changed during validation; no release evidence recorded")
    receipt = {
        "schema": 1,
        "identity": current,
        "base_tree": base,
        "revision": revision,
        "image": image,
        "checks": dict.fromkeys([*groups, "static", "postgres"], "passed"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    temporary_receipt = target.with_suffix(".tmp")
    temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary_receipt.replace(target)
    print(
        json.dumps(
            {
                "validated_tree": current["tree"],
                "groups": groups,
                "image": image,
                "elapsed_seconds": receipt["elapsed_seconds"],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "validate", "verify", "image"])
    parser.add_argument("groups", nargs="*", default=[])
    args = parser.parse_args()
    try:
        if args.command == "check":
            check(args.groups or ["native"])
        elif args.command == "validate":
            validate()
        else:
            receipt = verify()
            print(
                receipt["image"]
                if args.command == "image"
                else json.dumps({"tree": receipt["identity"]["tree"], "image": receipt["image"]})
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"local CI: {exc}\n")


if __name__ == "__main__":
    main()

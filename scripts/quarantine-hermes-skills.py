"""Preserve runtime-authored protocols outside retrieval during a drained deployment."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path


def quarantine(runtime_root: Path) -> Path | None:
    root = runtime_root.resolve(strict=True)
    source = root / "skills"
    archive = root / "protocol-quarantine"
    if source.is_symlink() or archive.is_symlink():
        raise ValueError("Skill source and quarantine must not be symlinks")
    if not source.exists() or not any(source.iterdir()):
        return None
    archive.mkdir(mode=0o700, exist_ok=True)
    destination = archive / f"skills-{uuid.uuid4().hex}"
    # Same-filesystem atomic rename preserves all original files, including
    # unrecognized future formats. No compatibility decoder or deletion.
    source.rename(destination)
    source.mkdir(mode=0o700)
    return destination


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: quarantine-hermes-skills.py HERMES_RUNTIME_ROOT")
    result = quarantine(Path(sys.argv[1]))
    print(json.dumps({"quarantined": result is not None, "path": str(result) if result else None}))


if __name__ == "__main__":
    main()

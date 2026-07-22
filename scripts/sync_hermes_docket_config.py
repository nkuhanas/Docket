#!/usr/bin/env python3
"""Synchronize only Docket's managed MCP tool allowlist into an active Hermes config."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


class HermesConfigSyncError(RuntimeError):
    pass


def _tool_block(document: str) -> str:
    marker = "    tools:\n      include:\n"
    try:
        suffix = document.split(marker, 1)[1]
        include = suffix.split("      prompts:", 1)[0]
    except IndexError as exc:
        raise HermesConfigSyncError("Docket MCP tool block was not found") from exc
    if document.count(marker) != 1 or suffix.count("      prompts:") != 1:
        raise HermesConfigSyncError("Docket MCP tool block is ambiguous")
    lines = [line for line in include.splitlines() if line.strip()]
    if not lines or any(not line.startswith("        - docket_") for line in lines):
        raise HermesConfigSyncError("Docket MCP tool block contains an unmanaged entry")
    return "\n".join(lines) + "\n"


def synchronize(active: str, template: str) -> str:
    marker = "    tools:\n      include:\n"
    desired = _tool_block(template)
    _tool_block(active)
    prefix, suffix = active.split(marker, 1)
    _old, tail = suffix.split("      prompts:", 1)
    return f"{prefix}{marker}{desired}      prompts:{tail}"


def atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: sync_hermes_docket_config.py ACTIVE_CONFIG TEMPLATE_CONFIG",
            file=sys.stderr,
        )
        return 2
    active_path = Path(sys.argv[1])
    template_path = Path(sys.argv[2])
    try:
        updated = synchronize(
            active_path.read_text(encoding="utf-8"),
            template_path.read_text(encoding="utf-8"),
        )
        atomic_write(active_path, updated)
    except (OSError, HermesConfigSyncError) as exc:
        print(f"Hermes Docket config sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

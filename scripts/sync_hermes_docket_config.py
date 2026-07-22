#!/usr/bin/env python3
"""Synchronize Docket-managed Hermes tools and channel-lane policy."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path


class HermesConfigSyncError(RuntimeError):
    pass


def _section_bounds(document: str, header: str, *, indent: int = 0) -> tuple[int, int]:
    lines = document.splitlines(keepends=True)
    marker = f"{' ' * indent}{header}:"
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == marker]
    if len(matches) != 1:
        raise HermesConfigSyncError(f"Hermes {header} section is missing or ambiguous")
    start_line = matches[0]
    end_line = len(lines)
    for index in range(start_line + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = len(lines[index]) - len(lines[index].lstrip(" "))
        if leading <= indent:
            end_line = index
            break
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets[start_line], offsets[end_line]


def _nested_section(document: str, parent: str, child: str) -> tuple[int, int]:
    parent_start, parent_end = _section_bounds(document, parent)
    parent_text = document[parent_start:parent_end]
    child_start, child_end = _section_bounds(parent_text, child, indent=2)
    return parent_start + child_start, parent_start + child_end


def _replace_discord_toolset(active: str, template: str) -> str:
    active_start, active_end = _nested_section(active, "platform_toolsets", "discord")
    template_start, template_end = _nested_section(template, "platform_toolsets", "discord")
    desired = template[template_start:template_end]
    if re.search(r"(?m)^\s*- cronjob\s*$", desired):
        raise HermesConfigSyncError("Template Discord toolset must not expose cronjob")
    return f"{active[:active_start]}{desired}{active[active_end:]}"


def _sync_display_policy(active: str, template: str) -> str:
    managed = (
        "tool_progress",
        "interim_assistant_messages",
        "show_commentary",
        "background_process_notifications",
    )
    active_start, active_end = _section_bounds(active, "display")
    template_start, template_end = _section_bounds(template, "display")
    active_block = active[active_start:active_end]
    template_block = template[template_start:template_end]
    for key in managed:
        pattern = re.compile(rf"(?m)^  {re.escape(key)}:[^\r\n]*(?:\r?\n|$)")
        desired = pattern.findall(template_block)
        existing = pattern.findall(active_block)
        if len(desired) != 1 or len(existing) > 1:
            raise HermesConfigSyncError(f"Hermes display.{key} is missing or ambiguous")
        if existing:
            active_block = pattern.sub(desired[0], active_block, count=1)
        else:
            header_end = active_block.find("\n") + 1
            active_block = f"{active_block[:header_end]}{desired[0]}{active_block[header_end:]}"
    return f"{active[:active_start]}{active_block}{active[active_end:]}"


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
    updated = f"{prefix}{marker}{desired}      prompts:{tail}"
    updated = _sync_display_policy(updated, template)
    return _replace_discord_toolset(updated, template)


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

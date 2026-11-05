"""Regenerate the native plugin reference contract."""

from pathlib import Path

from docket.tool_contracts import render_contract

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    plugin_contract = ROOT / "plugin" / "docket" / "contracts" / "plugin.md"
    plugin_contract.parent.mkdir(parents=True, exist_ok=True)
    plugin_contract.write_text(render_contract("plugin"), encoding="utf-8")


if __name__ == "__main__":
    main()

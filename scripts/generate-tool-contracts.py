"""Regenerate the native plugin reference contract."""

from pathlib import Path

from docket.schemas.business import public_effect_tools
from docket.tool_contracts import render_contract

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    plugin_contract = ROOT / "plugin" / "docket" / "contracts" / "plugin.md"
    plugin_contract.parent.mkdir(parents=True, exist_ok=True)
    plugin_contract.write_text(render_contract("plugin"), encoding="utf-8")
    effects = public_effect_tools()
    disposition = [
        "# Native capability disposition", "",
        "Generated from current canonical definitions and their native public routes.",
        f"All {len(effects)} canonical mutation variants have an advertised route.", "",
        "| Internal effect | Native business tool |", "| --- | --- |",
        *(f"| `{effect}` | `{tool}` |" for effect, tool in effects.items()), "",
        "Shared retraction dispatches the existing typed handler from the target reference.",
        "Identity unbinding and Calendar cancellation retain their distinct domain semantics.",
        "Retired compatibility aliases are unregistered; historical records remain readable.", "",
        "Timer transitions also have named resume/stop tools; switching is one atomic effect.", "",
        "Conflict and attention decisions retain observed versions and exact proposals.",
        "Capture is optional. Batching remains one atomic canonical/provider transaction.",
        "Saved work retains exact historical revisions and authentic roots.", "",
    ]
    (ROOT / "docs/native-capabilities.md").write_text("\n".join(disposition), encoding="utf-8")


if __name__ == "__main__":
    main()

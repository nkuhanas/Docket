"""Regenerate the validated Markdown contracts injected into Hermes sessions."""

from pathlib import Path

from docket.tool_contracts import render_contract

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIRECTORY = ROOT / "hermes" / "plugin" / "docket_discord" / "contracts"


def main() -> None:
    CONTRACT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for profile in ("interactive", "triage"):
        (CONTRACT_DIRECTORY / f"{profile}.md").write_text(
            render_contract(profile),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

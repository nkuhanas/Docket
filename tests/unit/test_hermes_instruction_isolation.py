import importlib.util
from pathlib import Path

import pytest
import yaml


def _quarantine_module():
    spec = importlib.util.spec_from_file_location(
        "quarantine_skills", "scripts/quarantine-hermes-skills.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_protocols_are_recoverably_quarantined(tmp_path) -> None:
    root = tmp_path / "skills" / "productivity" / "old-schedule"
    root.mkdir(parents=True)
    evidence = root / "SKILL.md"
    evidence.write_text("Old runtime recipe, retained as evidence.")
    module = _quarantine_module()
    archived = module.quarantine(tmp_path)
    assert (archived / "productivity/old-schedule/SKILL.md").read_text() == (
        "Old runtime recipe, retained as evidence."
    )
    assert list((tmp_path / "skills").iterdir()) == []
    assert module.quarantine(tmp_path) is None


def test_quarantine_rejects_redirected_roots(tmp_path) -> None:
    (tmp_path / "skills").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlinks"):
        _quarantine_module().quarantine(tmp_path)


def test_entire_discovered_skill_root_is_read_only_and_has_no_external_dirs() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    volumes = compose["services"]["hermes"]["volumes"]
    assert "./hermes/plugin/docket_discord/skills:/opt/data/skills:ro" in volumes
    config = yaml.safe_load(Path("hermes/config.example.yaml").read_text())
    assert config["skills"] == {"external_dirs": []}
    script = Path("scripts/docket").read_text()
    action = script.index('python3 "$ROOT/scripts/quarantine-hermes-skills.py"')
    assert script.index("wait_for_operational_idle", script.index("deploy()")) < action
    assert action < script.index("compose up -d --no-build --force-recreate docket hermes", action)

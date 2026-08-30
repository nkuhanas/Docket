from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from docket.config import clear_settings_cache
from docket.models import Base

RETIRED_TABLES = {
    "accounts",
    "actions",
    "action_revisions",
    "agent_response_projections",
    "approvals",
    "calendar_links",
    "calendar_profiles",
    "calendar_reminder_plans",
    "command_requests",
    "discord_mcp_traces",
    "discord_projections",
    "entity_relations",
    "event_observations",
    "operation_items",
    "organization_profiles",
    "provenance_sources",
    "queue_items",
    "queue_item_sources",
    "records",
    "record_sources",
    "reminder_rules",
    "semantic_candidates",
    "semantic_prompt_projections",
    "source_items",
    "triage_brief_entries",
}


def _config(database_url: str, monkeypatch) -> Config:
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    return Config("alembic.ini")


def test_active_migration_history_is_one_clean_baseline() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())

    assert len(revisions) == 1
    assert revisions[0].revision == "2022877699cf"
    assert revisions[0].down_revision is None
    assert script.get_current_head() == revisions[0].revision


def test_clean_baseline_matches_current_metadata(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'clean-baseline.db'}"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    migrated_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}

    assert migrated_tables == set(Base.metadata.tables)
    assert not RETIRED_TABLES.intersection(migrated_tables)
    assert {
        "provider_accounts",
        "sources",
        "items",
        "tasks",
        "temporal_bindings",
        "canonical_events",
        "attention_cases",
        "case_items",
        "operator_projections",
        "persisted_semantic_options",
        "operation_targets",
        "conversational_tool_traces",
    }.issubset(migrated_tables)

    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)

    engine.dispose()
    clear_settings_cache()


def test_clean_namespace_columns_are_unambiguous(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'clean-namespace.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)

    assert "ref_id" in {column["name"] for column in inspector.get_columns("items")}
    assert "ref_id" in {column["name"] for column in inspector.get_columns("case_items")}
    assert "projection_ref" in {
        column["name"] for column in inspector.get_columns("persisted_semantic_options")
    }
    assert "selected_option_ref" in {
        column["name"] for column in inspector.get_columns("operator_utterances")
    }
    invocation_columns = {column["name"] for column in inspector.get_columns("tool_invocations")}
    assert {
        "trace_ref",
        "transport_state",
        "domain_state",
        "result_disposition",
    }.issubset(invocation_columns)
    assert "status" not in invocation_columns
    assert "lease_key" in {column["name"] for column in inspector.get_columns("execution_leases")}
    assert "ref_id" not in {column["name"] for column in inspector.get_columns("execution_leases")}

    engine.dispose()
    clear_settings_cache()

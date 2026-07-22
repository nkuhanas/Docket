import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from docket.config import clear_settings_cache


@pytest.mark.integration
def test_initial_migration_upgrades_and_downgrades(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DOCKET_DATABASE_URL", database_url)
    clear_settings_cache()
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert {
        "accounts",
        "records",
        "record_sources",
        "command_requests",
        "outbox_events",
        "audit_events",
    }.issubset(set(inspect(engine).get_table_names()))

    command.downgrade(config, "base")
    assert "records" not in inspect(engine).get_table_names()
    engine.dispose()
    clear_settings_cache()

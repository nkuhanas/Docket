import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SECRETS = ROOT / "secrets" / "smoke"

# Tests must never inherit production settings or credential paths from .env.
os.environ.update(
    {
        "DOCKET_ENVIRONMENT": "test",
        "DOCKET_DATABASE_URL": f"sqlite+pysqlite:///{ROOT / '.runtime/test-bootstrap.db'}",
        "DOCKET_CALENDAR_READS_ENABLED": "false",
        "DOCKET_EXTERNAL_WRITES_ENABLED": "false",
        "DOCKET_AUTO_CREATE_SCHEMA": "false",
        "DOCKET_OPERATOR_DISCORD_USER_ID": "000000000000000001",
        "DOCKET_DISCORD_GUILD_ID": "000000000000000002",
        "DOCKET_CHAT_CHANNEL_ID": "000000000000000003",
        "DOCKET_QUEUE_CHANNEL_ID": "000000000000000004",
        "DOCKET_SYSTEM_CHANNEL_ID": "000000000000000005",
        "DOCKET_TO_HERMES_TOKEN_FILE": str(SMOKE_SECRETS / "docket_to_hermes_token"),
        "HERMES_TO_DOCKET_TOKEN_FILE": str(SMOKE_SECRETS / "hermes_to_docket_token"),
        "DOCKET_INTERACTION_SIGNING_KEY_FILE": str(SMOKE_SECRETS / "interaction_signing_key"),
        "GOOGLE_OAUTH_CLIENT_FILE": str(SMOKE_SECRETS / "google_oauth_client.json"),
        "GOOGLE_OAUTH_TOKEN_FILE": str(SMOKE_SECRETS / "google_oauth_token.json"),
    }
)

from docket.database import configure_database, get_session_factory  # noqa: E402
from docket.models import Base  # noqa: E402


@pytest.fixture
def session_factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    engine = configure_database(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield get_session_factory()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    database_session = session_factory()
    try:
        yield database_session
    finally:
        database_session.close()

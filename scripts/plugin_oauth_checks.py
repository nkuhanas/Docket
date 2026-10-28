"""PostgreSQL-only retained OAuth identity and migration guard checks."""

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from docket.config import get_settings
from docket.services.oauth import OAuthPrincipalService


def test_oauth_identity_is_retained_in_postgresql(factory) -> None:
    settings = get_settings().model_copy(update={"plugin_oauth_issuer": "https://identity.test/"})
    with factory.begin() as session:
        principal = OAuthPrincipalService(session, settings).approve("synthetic-owner", events=True)
        ref = principal.ref_id
    for statement in (
        "UPDATE oauth_principals SET subject = 'other-owner' WHERE ref_id = :ref",
        "DELETE FROM oauth_principals WHERE ref_id = :ref",
    ):
        try:
            with factory.begin() as session:
                session.execute(text(statement), {"ref": ref})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL discarded OAuth identity evidence")
    with factory.begin() as session:
        assert not OAuthPrincipalService(session, settings).revoke(ref).enabled
    migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261028a1b2")
    assert migration is not None
    try:
        with (
            factory.kw["bind"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.module.downgrade()
    except RuntimeError as exc:
        assert "retained" in str(exc)
    else:
        raise AssertionError("Downgrade discarded enrolled OAuth principals")

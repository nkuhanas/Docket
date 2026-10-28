import pytest
from pydantic import ValidationError

from docket.config import Settings, get_settings
from docket.plugin_auth_cli import main


def configured(**updates):
    values = get_settings().model_dump(by_alias=True)
    values.update({
        "DOCKET_PLUGIN_ENABLED": True,
        "DOCKET_PLUGIN_OAUTH_ISSUER": "https://identity.example.test/",
        "DOCKET_PLUGIN_OAUTH_JWKS_URL": "https://identity.example.test/.well-known/jwks.json",
        "DOCKET_PLUGIN_OAUTH_CLIENT_IDS": ["predefined-client"],
    })
    values.update(updates)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("updates", [
    {"DOCKET_PLUGIN_OAUTH_JWKS_URL": "https://other.example.test/keys"},
    {"DOCKET_PLUGIN_OAUTH_JWKS_URL": "http://identity.example.test/keys"},
    {"DOCKET_PLUGIN_OAUTH_CLIENT_IDS": []},
    {"DOCKET_PLUGIN_PUBLIC_ORIGIN": "https://docket.you/private"},
    {"DOCKET_PLUGIN_EVENTS_SCOPE": "docket.read"},
    {"DOCKET_PLUGIN_READ_SCOPE": "read write"},
])
def test_plugin_configuration_rejects_incomplete_or_unsafe_connection(updates):
    with pytest.raises(ValidationError):
        configured(**updates)


def test_production_can_use_an_explicit_identity_without_discord():
    settings = configured(
        DOCKET_ENVIRONMENT="production", DOCKET_DISCORD_ADAPTER_ENABLED=False,
        DOCKET_OPERATOR_REF="operator:personal",
    )
    assert settings.canonical_operator_ref == "operator:personal"
    with pytest.raises(ValidationError):
        configured(
            DOCKET_ENVIRONMENT="production", DOCKET_DISCORD_ADAPTER_ENABLED=False,
            DOCKET_OPERATOR_REF=None,
        )


def test_administration_defaults_to_preview_without_opening_database(monkeypatch, capsys):
    monkeypatch.setattr(
        "docket.plugin_auth_cli.configure_database", lambda _: pytest.fail("DB write")
    )
    assert main(["approve", "--subject", "synthetic-user", "--events"]) == 0
    assert '"executed": false' in capsys.readouterr().out

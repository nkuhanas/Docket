import stat

import pytest

from docket.production_config import (
    ProductionConfigError,
    configure_database_credentials,
    configure_searxng_secret,
)


def test_configure_database_credentials_is_atomic_and_secret(tmp_path) -> None:
    env_file = tmp_path / ".env"
    credentials_dir = tmp_path / "secrets" / "local"
    env_file.write_text(
        "DOCKET_CREDENTIALS_DIR=./secrets/smoke\n"
        "DOCKET_DATABASE_URL=postgresql+psycopg://docket:docket-smoke@postgres:5432/docket\n",
        encoding="utf-8",
    )

    password_file = configure_database_credentials(
        env_file=env_file,
        credentials_dir=credentials_dir,
        rotate=False,
    )

    password = password_file.read_text(encoding="utf-8").strip()
    content = env_file.read_text(encoding="utf-8")
    assert len(password) == 64
    assert "DOCKET_CREDENTIALS_DIR=./secrets/local" in content
    assert "DOCKET_UID=" in content
    assert "DOCKET_GID=" in content
    assert f"POSTGRES_PASSWORD={password}" in content
    assert f"postgresql+psycopg://docket:{password}@postgres:5432/docket" in content
    assert "docket-smoke" not in content
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_existing_password_is_reused_without_rotation(tmp_path) -> None:
    env_file = tmp_path / ".env"
    credentials_dir = tmp_path / "secrets" / "local"
    credentials_dir.mkdir(parents=True)
    password_file = credentials_dir / "postgres_password"
    password_file.write_text(f"{'a' * 64}\n", encoding="utf-8")
    env_file.write_text("DOCKET_ENVIRONMENT=production\n", encoding="utf-8")

    configure_database_credentials(
        env_file=env_file,
        credentials_dir=credentials_dir,
        rotate=False,
    )
    assert password_file.read_text(encoding="utf-8").strip() == "a" * 64


def test_duplicate_environment_keys_are_rejected(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=one\nPOSTGRES_PASSWORD=two\n", encoding="utf-8")
    with pytest.raises(ProductionConfigError, match="Duplicate"):
        configure_database_credentials(
            env_file=env_file,
            credentials_dir=tmp_path / "secrets" / "local",
            rotate=False,
        )


def test_searxng_secret_is_generated_and_reused(tmp_path) -> None:
    credentials_dir = tmp_path / "secrets" / "local"

    secret_file = configure_searxng_secret(
        credentials_dir=credentials_dir,
        rotate=False,
    )
    original = secret_file.read_text(encoding="utf-8")

    configure_searxng_secret(credentials_dir=credentials_dir, rotate=False)

    assert len(original.strip()) == 64
    assert secret_file.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


def test_invalid_existing_searxng_secret_is_rejected(tmp_path) -> None:
    credentials_dir = tmp_path / "secrets" / "local"
    credentials_dir.mkdir(parents=True)
    (credentials_dir / "searxng_secret").write_text("too-short\n", encoding="utf-8")

    with pytest.raises(ProductionConfigError, match="SearXNG"):
        configure_searxng_secret(credentials_dir=credentials_dir, rotate=False)

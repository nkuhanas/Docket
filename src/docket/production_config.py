from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

_KEY_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_SECRET_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProductionConfigError(RuntimeError):
    """A safe-to-display production configuration error."""


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _replace_env_values(content: str, replacements: Mapping[str, str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for line in content.splitlines():
        match = _KEY_PATTERN.match(line)
        if match is not None and match.group(1) in replacements:
            key = match.group(1)
            if key in seen:
                raise ProductionConfigError(f"Duplicate .env key: {key}")
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def configure_database_credentials(
    *,
    env_file: Path,
    credentials_dir: Path,
    rotate: bool,
) -> Path:
    if not env_file.is_file():
        raise ProductionConfigError(f"Environment file not found: {env_file}")

    password_file = credentials_dir / "postgres_password"
    if password_file.exists() and not rotate:
        password = password_file.read_text(encoding="utf-8").strip()
        if not _SECRET_PATTERN.fullmatch(password):
            raise ProductionConfigError(
                f"Existing PostgreSQL password file is invalid: {password_file}"
            )
    else:
        password = secrets.token_hex(32)

    credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(credentials_dir, 0o700)
    _atomic_write(password_file, f"{password}\n", 0o600)

    database_url = f"postgresql+psycopg://docket:{password}@postgres:5432/docket"
    current = env_file.read_text(encoding="utf-8")
    updated = _replace_env_values(
        current,
        {
            "DOCKET_CREDENTIALS_DIR": "./secrets/local",
            "DOCKET_UID": str(os.getuid()),
            "DOCKET_GID": str(os.getgid()),
            "POSTGRES_PASSWORD": password,
            "DOCKET_DATABASE_URL": database_url,
        },
    )
    _atomic_write(env_file, updated, 0o600)
    return password_file


def configure_searxng_secret(*, credentials_dir: Path, rotate: bool) -> Path:
    secret_file = credentials_dir / "searxng_secret"
    if secret_file.exists() and not rotate:
        secret = secret_file.read_text(encoding="utf-8").strip()
        if not _SECRET_PATTERN.fullmatch(secret):
            raise ProductionConfigError(
                f"Existing SearXNG secret file is invalid: {secret_file}"
            )
    else:
        secret = secrets.token_hex(32)

    credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(credentials_dir, 0o700)
    _atomic_write(secret_file, f"{secret}\n", 0o600)
    return secret_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docket-production-config",
        description="Generate and install local production service credentials.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--credentials-dir", type=Path, default=Path("secrets/local"))
    parser.add_argument("--rotate", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        password_file = configure_database_credentials(
            env_file=arguments.env_file,
            credentials_dir=arguments.credentials_dir,
            rotate=arguments.rotate,
        )
        searxng_secret_file = configure_searxng_secret(
            credentials_dir=arguments.credentials_dir,
            rotate=arguments.rotate,
        )
    except (OSError, ProductionConfigError) as exc:
        print(f"Production credential setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Production database credentials installed; password stored at {password_file}")
    print(f"SearXNG service secret installed at {searxng_secret_file}")
    return 0

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from psycopg import sql
from sqlalchemy import create_engine

_INGRESS_ROLE = "docket_ingress"
_READ_TABLES = (
    "attachment_evidence_metadata",
    "deferred_ingress",
    "discord_daily_threads",
    "drain_barriers",
    "encrypted_attachment_blobs",
    "operator_projections",
    "operator_utterances",
    "persisted_semantic_options",
    "sources",
)
_APPEND_TABLES = (
    "attachment_evidence_metadata",
    "deferred_ingress",
    "encrypted_attachment_blobs",
    "operator_utterances",
    "sources",
)


def provision_ingress_role(database_url: str, password: str) -> None:
    """Create the least-privilege ingress login after schema migration."""

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        raw = engine.raw_connection()
        try:
            connection = cast(Any, raw.driver_connection)
            if connection is None:
                raise RuntimeError("PostgreSQL driver connection is unavailable")
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_INGRESS_ROLE,))
                exists = cursor.fetchone() is not None
                statement = sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(_INGRESS_ROLE), sql.Literal(password)
                )
                if not exists:
                    statement = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(_INGRESS_ROLE), sql.Literal(password)
                    )
                cursor.execute(statement)
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(connection.info.dbname), sql.Identifier(_INGRESS_ROLE)
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        sql.Identifier(_INGRESS_ROLE)
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT SELECT ON {} TO {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier("public", table_name) for table_name in _READ_TABLES
                        ),
                        sql.Identifier(_INGRESS_ROLE),
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT INSERT ON {} TO {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier("public", table_name) for table_name in _APPEND_TABLES
                        ),
                        sql.Identifier(_INGRESS_ROLE),
                    )
                )
                cursor.execute(
                    sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON {} FROM {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier("public", table_name) for table_name in _READ_TABLES
                        ),
                        sql.Identifier(_INGRESS_ROLE),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT UPDATE (ingest_state, retention_disposition, content_hash) "
                        "ON {} TO {}"
                    ).format(
                        sql.Identifier("public", "attachment_evidence_metadata"),
                        sql.Identifier(_INGRESS_ROLE),
                    )
                )
            connection.commit()
        finally:
            raw.close()
    finally:
        engine.dispose()


def main() -> None:
    password_file = Path(os.environ["DOCKET_INGRESS_DB_PASSWORD_FILE"])
    password = password_file.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("DOCKET_INGRESS_DB_PASSWORD_FILE is empty")
    provision_ingress_role(os.environ["DOCKET_DATABASE_URL"], password)

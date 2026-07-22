#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

export DOCKET_ENVIRONMENT=test
export DOCKET_DATABASE_URL="sqlite+pysqlite:///$ROOT/.runtime/smoke.db"
export DOCKET_AUTO_CREATE_SCHEMA=true
export DOCKET_CALENDAR_READS_ENABLED=false
export DOCKET_EXTERNAL_WRITES_ENABLED=false
export DOCKET_TO_HERMES_TOKEN_FILE="$ROOT/secrets/smoke/docket_to_hermes_token"
export HERMES_TO_DOCKET_TOKEN_FILE="$ROOT/secrets/smoke/hermes_to_docket_token"
export DOCKET_INTERACTION_SIGNING_KEY_FILE="$ROOT/secrets/smoke/interaction_signing_key"
export GOOGLE_OAUTH_CLIENT_FILE="$ROOT/secrets/smoke/google_oauth_client.json"
export GOOGLE_OAUTH_TOKEN_FILE="$ROOT/secrets/smoke/google_oauth_token.json"

uv run pytest
uv run ruff check .
uv run mypy

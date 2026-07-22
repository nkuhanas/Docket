#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CREDENTIALS_DIR=${DOCKET_CREDENTIALS_DIR:-"$ROOT/secrets/local"}

cd "$ROOT"
exec uv run docket-google-auth setup --credentials-dir "$CREDENTIALS_DIR" "$@"

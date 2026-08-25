#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CREDENTIALS_DIR=${DOCKET_CREDENTIALS_DIR:-"$ROOT/secrets/local"}
browser_mode_set=false

for argument in "$@"; do
    case "$argument" in
        --remote|--no-browser)
            browser_mode_set=true
            ;;
    esac
done

if [ "$browser_mode_set" = false ] &&
    [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${BROWSER:-}" ]; then
    printf '%s\n' \
        'No local browser session detected; selecting remote OAuth mode.' \
        'No SSH tunnel is required; follow the callback-URL paste prompt.' >&2
    set -- --remote "$@"
fi

cd "$ROOT"
exec uv run docket-google-auth setup --credentials-dir "$CREDENTIALS_DIR" "$@"

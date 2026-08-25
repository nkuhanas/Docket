#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_HOME_DIR=${DOCKET_HERMES_HOME:-"$ROOT/.runtime/hermes"}
MODE=main
TARGET=
AUTH_PATH=
AUTH_BACKUP=
AUTH_EXISTED=false
RESTORE_PENDING=false

usage() {
    cat <<'EOF'
Usage: scripts/setup-hermes-oauth.sh [--main | --triage | --all]

Reauthorize Hermes's OpenAI Codex model credential with the remote-friendly
device-code flow. The main and docket-triage profiles use independent OAuth
sessions; --all therefore requires two browser approvals.

Options:
  --main    Repair only interactive/main Hermes (default).
  --triage  Repair only the isolated docket-triage profile.
  --all     Repair both profiles using separate OAuth sessions.
  -h, --help
EOF
}

case "${1:-}" in
    ""|--main) MODE=main ;;
    --triage) MODE=triage ;;
    --all) MODE=all ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

compose() {
    if docker info >/dev/null 2>&1; then
        docker compose --profile hermes "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
        sudo -n docker compose --profile hermes "$@"
    else
        echo "Docker is unavailable; grant access or run 'sudo -v' first." >&2
        return 1
    fi
}

hermes() {
    case "$TARGET" in
        main) compose exec -T hermes hermes "$@" ;;
        triage) compose exec -T hermes hermes -p docket-triage "$@" ;;
        *) echo "Unknown Hermes OAuth target: $TARGET" >&2; return 2 ;;
    esac
}

cleanup() {
    if [ "$RESTORE_PENDING" = true ]; then
        if [ "$AUTH_EXISTED" = true ]; then
            cp "$AUTH_BACKUP" "$AUTH_PATH"
            chmod 600 "$AUTH_PATH"
        else
            rm -f "$AUTH_PATH"
        fi
        echo "Restored the prior $TARGET Hermes credential after an incomplete login." >&2
        RESTORE_PENDING=false
    fi
    if [ -n "$AUTH_BACKUP" ]; then
        rm -f "$AUTH_BACKUP"
        AUTH_BACKUP=
    fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

reauthorize() {
    TARGET=$1
    case "$TARGET" in
        main)
            AUTH_PATH="$HERMES_HOME_DIR/auth.json"
            label=docket-main
            ;;
        triage)
            if [ ! -d "$HERMES_HOME_DIR/profiles/docket-triage" ]; then
                echo "The docket-triage Hermes profile is not installed." >&2
                echo "Run scripts/docket setup-triage before repairing its OAuth." >&2
                return 1
            fi
            AUTH_PATH="$HERMES_HOME_DIR/profiles/docket-triage/auth.json"
            label=docket-triage
            ;;
    esac

    mkdir -p "$(dirname -- "$AUTH_PATH")"
    AUTH_BACKUP=$(mktemp "$AUTH_PATH.backup.XXXXXX")
    AUTH_EXISTED=false
    if [ -f "$AUTH_PATH" ]; then
        cp "$AUTH_PATH" "$AUTH_BACKUP"
        AUTH_EXISTED=true
    fi
    chmod 600 "$AUTH_BACKUP"
    RESTORE_PENDING=true

    printf '\nReauthorizing %s Hermes with a dedicated OpenAI Codex session.\n' "$TARGET"
    printf '%s\n' \
        'Open the device URL on any computer, enter the displayed code, and' \
        'leave this command running until Hermes confirms the token exchange.'

    hermes auth logout openai-codex
    hermes auth add openai-codex \
        --type oauth \
        --label "$label" \
        --no-browser
    hermes auth reset openai-codex
    status=$(hermes auth status openai-codex)
    printf '%s\n' "$status"
    printf '%s\n' "$status" | grep -F 'openai-codex: logged in' >/dev/null || {
        echo "$TARGET Hermes did not report a usable Codex credential." >&2
        return 1
    }

    RESTORE_PENDING=false
    rm -f "$AUTH_BACKUP"
    AUTH_BACKUP=
    echo "Reauthorized $TARGET Hermes."
}

case "$MODE" in
    main) reauthorize main ;;
    triage) reauthorize triage ;;
    all)
        reauthorize main
        reauthorize triage
        ;;
esac

echo "Restarting Hermes so the Discord gateway drops cached provider state."
compose restart hermes >/dev/null

attempt=0
until compose exec -T hermes hermes cron status 2>/dev/null | \
    grep -F 'Gateway is running' >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "Hermes restarted, but its gateway did not become ready within 60 seconds." >&2
        exit 1
    fi
    sleep 2
done

echo "Hermes OAuth recovery complete; the Discord gateway and cron ticker are running."

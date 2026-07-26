#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_HOME_DIR=${DOCKET_HERMES_HOME:-"$ROOT/.runtime/hermes"}
PROFILE_NAME=docket-triage
PROFILE_DIR="$HERMES_HOME_DIR/profiles/$PROFILE_NAME"
PROFILE_CONFIG="$PROFILE_DIR/config.yaml"
PROFILE_ENV="$PROFILE_DIR/.env"
PROFILE_SKILL_DIR="$PROFILE_DIR/skills/docket-triage"
JOB_NAME="Docket Gmail triage"

if [ ! -s "$HERMES_HOME_DIR/config.yaml" ] || [ ! -s "$HERMES_HOME_DIR/.env" ]; then
    echo "Prepare the primary Hermes home before installing triage." >&2
    exit 1
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

if [ ! -d "$PROFILE_DIR" ]; then
    compose exec -T hermes \
        hermes profile create "$PROFILE_NAME" --clone --no-alias
fi

if [ ! -d "$PROFILE_DIR" ]; then
    echo "Hermes did not create the expected profile directory: $PROFILE_DIR" >&2
    exit 1
fi

umask 077
config_tmp=$(mktemp "$PROFILE_DIR/.config.yaml.XXXXXX")
env_tmp=$(mktemp "$PROFILE_DIR/.env.XXXXXX")
trap 'rm -f "$config_tmp" "$env_tmp"' EXIT HUP INT TERM

cp "$ROOT/hermes/triage-config.example.yaml" "$config_tmp"
awk '
    !/^(DISCORD_|TELEGRAM_|SLACK_|WHATSAPP_|SIGNAL_|SMS_)/ &&
    !/^(HERMES_TO_DOCKET_|DOCKET_TO_HERMES_|DOCKET_INTERNAL_URL=)/
' "$HERMES_HOME_DIR/.env" > "$env_tmp"

if ! grep -q '^DOCKET_MCP_TOKEN=' "$env_tmp"; then
    echo "The isolated profile requires DOCKET_MCP_TOKEN." >&2
    exit 1
fi

mv "$config_tmp" "$PROFILE_CONFIG"
mv "$env_tmp" "$PROFILE_ENV"
rm -rf "$PROFILE_DIR/plugins" "$PROFILE_DIR/skills"
mkdir -p "$PROFILE_SKILL_DIR"
cp \
    "$ROOT/hermes/plugin/docket_discord/skills/docket-triage/SKILL.md" \
    "$PROFILE_SKILL_DIR/SKILL.md"
rm -f "$PROFILE_DIR/SOUL.md"
touch "$PROFILE_DIR/.no-skills"
chmod 700 "$PROFILE_DIR" "$PROFILE_DIR/skills" "$PROFILE_SKILL_DIR"
chmod 600 "$PROFILE_CONFIG" "$PROFILE_ENV" "$PROFILE_SKILL_DIR/SKILL.md"

job_listing=$(compose exec -T hermes env NO_COLOR=1 hermes cron list)
if ! printf '%s\n' "$job_listing" | grep -F "$JOB_NAME" >/dev/null; then
    compose exec -T hermes hermes cron create \
        "every 30m" \
        "Run the Docket Gmail triage skill now. Drain bounded claimed work and return [SILENT] after a normal run." \
        --profile "$PROFILE_NAME" \
        --skill docket-triage \
        --deliver log \
        --name "$JOB_NAME"
fi

compose exec -T hermes hermes -p "$PROFILE_NAME" mcp test docket-triage
compose exec -T hermes env NO_COLOR=1 hermes cron list
echo "Installed isolated Hermes profile and cron job: $JOB_NAME"

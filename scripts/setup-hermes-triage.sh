#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_HOME_DIR=${DOCKET_HERMES_HOME:-"$ROOT/.runtime/hermes"}
PROFILE_NAME=docket-triage
PROFILE_DIR="$HERMES_HOME_DIR/profiles/$PROFILE_NAME"
PROFILE_CONFIG="$PROFILE_DIR/config.yaml"
PROFILE_ENV="$PROFILE_DIR/.env"
PROFILE_SKILL_DIR="$PROFILE_DIR/skills/docket-triage"
PROFILE_CONTRACT="$PROFILE_DIR/tool-contract.md"
LAUNCHER_DIR="$HERMES_HOME_DIR/scripts"
LAUNCHER="$LAUNCHER_DIR/docket-gmail-triage.sh"
JOB_NAME="Docket Gmail triage"
JOB_SCHEDULE="every 5m"

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
mkdir -p "$PROFILE_SKILL_DIR" "$LAUNCHER_DIR"
cp \
    "$ROOT/hermes/plugin/docket_discord/skills/docket-triage/SKILL.md" \
    "$PROFILE_SKILL_DIR/SKILL.md"
cp "$ROOT/hermes/plugin/docket_discord/contracts/triage.md" "$PROFILE_CONTRACT"
cp "$ROOT/hermes/scripts/docket-gmail-triage.sh" "$LAUNCHER"
rm -f "$PROFILE_DIR/SOUL.md"
touch "$PROFILE_DIR/.no-skills"
chmod 700 \
    "$PROFILE_DIR" \
    "$PROFILE_DIR/skills" \
    "$PROFILE_SKILL_DIR" \
    "$LAUNCHER_DIR"
chmod 600 \
    "$PROFILE_CONFIG" \
    "$PROFILE_ENV" \
    "$PROFILE_SKILL_DIR/SKILL.md" \
    "$PROFILE_CONTRACT"
chmod 700 "$LAUNCHER"

job_listing=$(compose exec -T hermes env NO_COLOR=1 hermes cron list --all)
job_ids=$(printf '%s\n' "$job_listing" | awk -v wanted="$JOB_NAME" '
    /^[[:space:]]*[[:xdigit:]]{12}([[:space:]]|$)/ {
        candidate = $1
    }
    /^[[:space:]]+Name:/ {
        name = $0
        sub(/^[[:space:]]+Name:[[:space:]]*/, "", name)
        if (name == wanted) {
            print candidate
        }
    }
')
job_count=$(printf '%s\n' "$job_ids" | awk 'NF { count += 1 } END { print count + 0 }')
if [ "$job_count" -eq 0 ]; then
    compose exec -T hermes hermes cron create \
        "$JOB_SCHEDULE" \
        --script "docket-gmail-triage.sh" \
        --no-agent \
        --deliver local \
        --name "$JOB_NAME"
elif [ "$job_count" -eq 1 ]; then
    compose exec -T hermes hermes cron edit "$job_ids" \
        --schedule "$JOB_SCHEDULE" \
        --script "docket-gmail-triage.sh" \
        --no-agent \
        --deliver local \
        --name "$JOB_NAME"
else
    echo "Expected at most one '$JOB_NAME' cron job; found $job_count." >&2
    exit 1
fi

mcp_test_output=$(compose exec -T hermes hermes -p "$PROFILE_NAME" mcp test docket-triage)
printf '%s\n' "$mcp_test_output"
for expected_tool in \
    docket_get_triage_context \
    docket_submit_triage_analysis \
    docket_get_attention_case \
    docket_apply_existing_suppression
do
    printf '%s\n' "$mcp_test_output" | grep -F "$expected_tool" >/dev/null || {
        echo "The isolated profile did not discover $expected_tool." >&2
        exit 1
    }
done
discovered_tool_count=$(printf '%s\n' "$mcp_test_output" | grep -c '^    docket_' || true)
if [ "$discovered_tool_count" -ne 4 ]; then
    echo "The isolated profile must discover exactly four tools; found $discovered_tool_count." >&2
    exit 1
fi
job_listing=$(compose exec -T hermes env NO_COLOR=1 hermes cron list --all)
printf '%s\n' "$job_listing"
printf '%s\n' "$job_listing" | grep -F "$JOB_NAME" >/dev/null || {
    echo "The root gateway cannot see the Docket Gmail triage job." >&2
    exit 1
}
compose exec -T hermes hermes cron status | grep -F "Gateway is running" >/dev/null || {
    echo "The root Hermes gateway cron ticker is not running." >&2
    exit 1
}
echo "Installed isolated Hermes profile and cron job: $JOB_NAME"

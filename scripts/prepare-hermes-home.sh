#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ROOT_ENV_FILE=${DOCKET_ENV_FILE:-"$ROOT/.env"}
HERMES_HOME_DIR=${DOCKET_HERMES_HOME:-"$ROOT/.runtime/hermes"}

if [ ! -f "$ROOT_ENV_FILE" ]; then
    echo "Missing environment file: $ROOT_ENV_FILE" >&2
    exit 1
fi

read_env() {
    awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ROOT_ENV_FILE"
}

configured_credentials_dir=$(read_env DOCKET_CREDENTIALS_DIR)
case "$configured_credentials_dir" in
    /*) default_credentials_dir=$configured_credentials_dir ;;
    *) default_credentials_dir="$ROOT/${configured_credentials_dir#./}" ;;
esac
CREDENTIALS_DIR=${DOCKET_CREDENTIALS_DIR:-"$default_credentials_dir"}

operator_id=$(read_env DOCKET_OPERATOR_DISCORD_USER_ID)
guild_id=$(read_env DOCKET_DISCORD_GUILD_ID)
chat_channel_id=$(read_env DOCKET_CHAT_CHANNEL_ID)
queue_channel_id=$(read_env DOCKET_QUEUE_CHANNEL_ID)
system_channel_id=$(read_env DOCKET_SYSTEM_CHANNEL_ID)

for value in "$operator_id" "$guild_id" "$chat_channel_id" "$queue_channel_id" "$system_channel_id"; do
    case "$value" in
        ''|*[!0-9]*|000000*)
            echo "Discord identifiers in $ROOT_ENV_FILE must be non-placeholder numeric IDs." >&2
            exit 1
            ;;
    esac
done

required="discord_bot_token docket_to_hermes_token hermes_to_docket_token"
for name in $required; do
    if [ ! -s "$CREDENTIALS_DIR/$name" ]; then
        echo "Missing credential file: $CREDENTIALS_DIR/$name" >&2
        exit 1
    fi
done

case "$(head -n 1 "$CREDENTIALS_DIR/discord_bot_token")" in
    dummy-*)
        echo "Refusing to prepare a live Hermes home with the dummy Discord token." >&2
        exit 1
        ;;
esac

mkdir -p "$HERMES_HOME_DIR"
chmod 700 "$HERMES_HOME_DIR"

umask 077
if [ ! -e "$HERMES_HOME_DIR/config.yaml" ]; then
    config_tmp=$(mktemp "$HERMES_HOME_DIR/.config.yaml.XXXXXX")
    sed \
        -e "s/000000000000000003/$chat_channel_id/g" \
        -e "s/000000000000000004/$queue_channel_id/g" \
        -e "s/000000000000000005/$system_channel_id/g" \
        "$ROOT/hermes/config.example.yaml" > "$config_tmp"
    mv "$config_tmp" "$HERMES_HOME_DIR/config.yaml"
fi
python3 "$ROOT/scripts/sync_hermes_docket_config.py" \
    "$HERMES_HOME_DIR/config.yaml" "$ROOT/hermes/config.example.yaml"

{
    echo "DISCORD_BOT_TOKEN=$(head -n 1 "$CREDENTIALS_DIR/discord_bot_token")"
    echo "DOCKET_MCP_TOKEN=$(head -n 1 "$CREDENTIALS_DIR/docket_to_hermes_token")"
    echo "HERMES_TO_DOCKET_TOKEN_FILE=/run/docket-secrets/hermes_to_docket_token"
    echo "DOCKET_INTERNAL_URL=http://docket:8000"
} > "$HERMES_HOME_DIR/.env"

chmod 600 "$HERMES_HOME_DIR/config.yaml" "$HERMES_HOME_DIR/.env"
echo "Prepared $HERMES_HOME_DIR with configured Discord channels. Run Hermes setup next."

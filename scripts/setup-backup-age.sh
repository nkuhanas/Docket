#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

rotate=false
if [[ "${1:-}" == "--rotate" ]]; then
    rotate=true
elif [[ -n "${1:-}" ]]; then
    printf 'Usage: scripts/setup-backup-age.sh [--rotate]\n' >&2
    exit 2
fi

docker_command=()
if docker info >/dev/null 2>&1; then
    docker_command=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    docker_command=(sudo -n docker)
else
    printf 'setup-backup-age: Docker is unavailable\n' >&2
    exit 1
fi

[[ -f .env ]] || {
    printf 'setup-backup-age: .env is missing\n' >&2
    exit 1
}
identity_dir="$ROOT/secrets/restore"
identity_file="$identity_dir/backup_age_identity"
mkdir -p "$identity_dir"
chmod 700 "$identity_dir"
if [[ -f "$identity_file" && "$rotate" != true ]]; then
    :
else
    rm -f "$identity_file"
    "${docker_command[@]}" run --rm \
        --user "$(id -u):$(id -g)" \
        -v "$identity_dir:/output" \
        docket-docket:latest \
        age-keygen --output /output/backup_age_identity
fi
chmod 600 "$identity_file"
recipient=$(
    "${docker_command[@]}" run --rm \
        --user "$(id -u):$(id -g)" \
        -v "$identity_file:/run/identity:ro" \
        docket-docket:latest \
        age-keygen -y /run/identity
)
uv run docket-production-config \
    --env-file "$ROOT/.env" \
    --backup-only \
    --backup-age-recipient "$recipient"
printf 'Private restore identity: %s\n' "$identity_file"
printf 'Set DOCKET_BACKUP_AGE_IDENTITY_FILE=%s only while verifying restore.\n' \
    "$identity_file"

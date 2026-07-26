#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

die() {
    printf 'verify-restore: %s\n' "$*" >&2
    exit 1
}

docker_command=()
if docker info >/dev/null 2>&1; then
    docker_command=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    docker_command=(sudo -n docker)
else
    die "Docker is unavailable"
fi

identity=${DOCKET_BACKUP_AGE_IDENTITY_FILE:-}
[[ -n "$identity" && -f "$identity" ]] ||
    die "DOCKET_BACKUP_AGE_IDENTITY_FILE must name the private age identity"
identity=$(realpath "$identity")

manifest=${1:-}
if [[ -z "$manifest" ]]; then
    manifest=$(find "$ROOT/backups" -maxdepth 1 -type f \
        -name 'docket-*.dump.age.manifest.json' -printf '%T@ %p\n' |
        sort -nr | awk 'NR == 1 { sub(/^[^ ]+ /, ""); print; exit }')
fi
[[ -n "$manifest" && -f "$manifest" ]] || die "encrypted backup manifest not found"
manifest=$(realpath "$manifest")
backup_dir=$(dirname "$manifest")
manifest_name=$(basename "$manifest")
backup_image=${DOCKET_BACKUP_IMAGE:-docket-docket:latest}

readarray -t metadata < <(
    python - "$manifest" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
artifact = str(payload["artifact_name"])
if pathlib.Path(artifact).name != artifact:
    raise SystemExit("manifest artifact_name is not a basename")
print(artifact)
print(str(payload["ciphertext_sha256"]))
print(str(payload["schema_revision"]))
PY
)
artifact_name=${metadata[0]:-}
expected_sha256=${metadata[1]:-}
expected_schema=${metadata[2]:-}
[[ -n "$artifact_name" && -f "$backup_dir/$artifact_name" ]] ||
    die "encrypted backup artifact is missing"
actual_sha256=$(sha256sum "$backup_dir/$artifact_name" | awk '{print $1}')
[[ "$actual_sha256" == "$expected_sha256" ]] ||
    die "encrypted backup checksum does not match its manifest"

suffix="$(date -u +%Y%m%dT%H%M%SZ)-$$"
container="docket-restore-postgres-$suffix"
network="docket-restore-$suffix"
volume="docket-restore-$suffix"
password="docket-restore-$suffix"
completed=false

record_result() {
    local status=$1
    local error_code=${2:-}
    if [[ "${DOCKET_RESTORE_RECORD_AUDIT:-true}" != "true" ]]; then
        return
    fi
    if "${docker_command[@]}" compose ps -q docket >/dev/null 2>&1 &&
        [[ -n "$("${docker_command[@]}" compose ps -q docket)" ]]; then
        local arguments=(
            ./.venv/bin/python -m docket.backup_cli record-restore
            --manifest-name "$manifest_name"
            --status "$status"
            --schema-revision "$expected_schema"
        )
        if [[ -n "$error_code" ]]; then
            arguments+=(--error-code "$error_code")
        fi
        "${docker_command[@]}" compose exec -T docket "${arguments[@]}" >/dev/null ||
            printf 'verify-restore: could not record %s audit result\n' "$status" >&2
    fi
}

cleanup() {
    local exit_code=$?
    "${docker_command[@]}" rm -f "$container" >/dev/null 2>&1 || true
    "${docker_command[@]}" volume rm "$volume" >/dev/null 2>&1 || true
    "${docker_command[@]}" network rm "$network" >/dev/null 2>&1 || true
    if [[ "$completed" != true ]]; then
        record_result failed restore_verification_failed
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

"${docker_command[@]}" network create "$network" >/dev/null
"${docker_command[@]}" volume create "$volume" >/dev/null
"${docker_command[@]}" run -d --name "$container" \
    --network "$network" \
    -e POSTGRES_DB=docket \
    -e POSTGRES_USER=docket \
    -e "POSTGRES_PASSWORD=$password" \
    -v "$volume:/var/lib/postgresql/data" \
    postgres:16.9-bookworm >/dev/null

for _attempt in {1..60}; do
    if "${docker_command[@]}" exec "$container" pg_isready -U docket -d docket \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
"${docker_command[@]}" exec "$container" pg_isready -U docket -d docket >/dev/null ||
    die "disposable PostgreSQL did not become ready"

"${docker_command[@]}" run --rm --user 0:0 \
    --network "$network" \
    -e "PGPASSWORD=$password" \
    -e "BACKUP_FILE=/backup/$artifact_name" \
    -e "DATABASE_TARGET=postgresql://docket@${container}:5432/docket" \
    -e "DOCKET_DATABASE_URL=postgresql+psycopg://docket:${password}@${container}:5432/docket" \
    -v "$backup_dir:/backup:ro" \
    -v "$identity:/run/restore/identity:ro" \
    "$backup_image" \
    sh -ec '
        age --decrypt --identity /run/restore/identity "$BACKUP_FILE" >/tmp/docket.dump
        pg_restore --exit-on-error --no-owner --no-privileges \
            --dbname "$DATABASE_TARGET" /tmp/docket.dump
        rm -f /tmp/docket.dump
        ./.venv/bin/alembic -x ignored=1 current | grep -F "(head)"
    ' >/dev/null

restored_schema=$(
    "${docker_command[@]}" exec -e "PGPASSWORD=$password" "$container" \
        psql -U docket -d docket -Atc "SELECT version_num FROM alembic_version"
)
[[ "$restored_schema" == "$expected_schema" ]] ||
    die "restored schema revision does not match the manifest"

"${docker_command[@]}" exec -e "PGPASSWORD=$password" "$container" \
    psql -U docket -d docket -v ON_ERROR_STOP=1 -Atc \
    "SELECT count(*) FROM command_requests;
     SELECT count(*) FROM audit_events;
     SELECT count(*) FROM operations
       WHERE status IN ('pending','running','reconciliation_required');" >/dev/null

completed=true
record_result succeeded
printf 'Verified encrypted restore: %s (schema %s)\n' \
    "$artifact_name" "$restored_schema"

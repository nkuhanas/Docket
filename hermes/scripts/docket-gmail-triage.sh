#!/bin/sh
set -eu

profile_home=${HERMES_HOME:-/opt/data}/profiles/docket-triage
session_dir=$profile_home/sessions
before_dumps=0
if [ -d "$session_dir" ]; then
    before_dumps=$(find "$session_dir" -maxdepth 1 -type f \
        -name 'request_dump_*.json' | wc -l)
fi

output_file=$(mktemp)
trap 'rm -f "$output_file"' EXIT HUP INT TERM

if ! hermes -p docket-triage \
    --skills docket-triage \
    --oneshot \
    "Run the Docket Gmail triage skill now. Process one claimed source and return [SILENT] after a normal run." \
    >"$output_file"
then
    echo "Docket Gmail triage failed before completion." >&2
    exit 1
fi

after_dumps=0
if [ -d "$session_dir" ]; then
    after_dumps=$(find "$session_dir" -maxdepth 1 -type f \
        -name 'request_dump_*.json' | wc -l)
fi
if [ "$after_dumps" -gt "$before_dumps" ]; then
    echo "Docket Gmail triage exhausted its model request retries." >&2
    exit 1
fi

cat "$output_file"

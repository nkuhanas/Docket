#!/bin/sh
set -eu

exec hermes -p docket-triage \
    --skills docket-triage \
    --oneshot \
    "Run the Docket Gmail triage skill now. Drain bounded claimed work and return [SILENT] after a normal run."

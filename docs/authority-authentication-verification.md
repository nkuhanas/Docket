# Authority and authentication verification

The Operator authorized the October 7 amendment in the development conversation
and removed the requirement to send Discord sign-off messages before local delta
implementation or repository work. AGENTS.md records that workflow. Historical
signed sources and ledger records keep their original provenance; development
approval does not manufacture a production Decision or authorize deployment.

The first implementation slice adds mandatory authenticated request records
(`req_`) and optional encrypted, append-only conversation records (`rec_`).
Request admission checks server-established principal identity, Operator binding,
role, service audience, current validity, and permission. Triage/read-only callers
cannot gain interactive authority by claiming permissions or copying a request.
Conversation capture uses a separate transaction and identifies relayed text as
agent-reported. Duplicate record keys require identical content; corrections
append a new record. These are action roots, not invented human utterances.

Migration `20261007a1b2` adds these records without updating historical evidence,
requests, drafts, governance records, or pending Operations. Downgrade refuses to
discard durable request evidence. PostgreSQL enforces immutable attribution and
append-only records as well as ORM guards.

The workflow slice binds sessions, statements, semantic requests, executions,
and operations directly to genuine authenticated requests. Migration
`20261007b2c3` leaves historical work unchanged and refuses a lossy downgrade
after request-backed work exists. New interpretations use version 2 request
specifications marked `agent_reported_interpretation`.

New requests permit corrected drafts within their admitted effect boundary.
Transcript capture, source registration, extraction locators, and first-reading
proofs do not gate them. Required domain values, selected-entry completeness,
exact targets, expected versions, occurrence scope, immutable revisions, and
compiler pins continue to apply. Commit consumes the authenticated request in
the same transaction as canonical effects and provider intents. Copied requests
or operation tokens do not grant background callers authority.

Integration with MCP and Hermes, retention, and operational role configuration
remains part of the active work.
The dot/plugin/MCP Events adapter is separate work.

Initial slice validation: `scripts/docket check` passed 984 tests, Ruff, and
strict mypy. `scripts/docket compose-smoke` passed isolated authenticated MCP,
governance restore, existing request/revision races and provider recovery,
new concurrent request admission and immutable attribution, and PostgreSQL
migration downgrade/re-upgrade. Tests used smoke credentials and synthetic data.
No production deployment or live provider operation was performed.

Workflow slice validation: the full gate passed 988 tests, Ruff, and strict
mypy. The isolated Compose smoke passed a request-backed stage/commit across
PostgreSQL connections, concurrent admission, immutable attribution, guarded
downgrade, and the existing provider, occurrence, revision, restore, and
migration round-trip checks.

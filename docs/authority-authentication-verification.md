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

MCP and Hermes now use request authority end to end. Server authentication assigns
interactive, triage, or read-only roles through distinct credentials. The foreground
bridge signs request correlation, keeps task ownership separate from shared session
identity, and records optional transcripts/files/responses independently of tool
execution and delivery. Direct interactive clients supply stable identities in
trusted HTTP metadata. Authentication and mandatory action audit remain fail-closed;
capture does not. Every authenticated tool call, including rejection, creates `call_`.

Migration `20261007c3d4` adds exact invocation-operation correlation, optional
request-bound attachments, admitted timezone, and irreversible transcript payload
purge. Metadata, hashes, attribution, immutable revisions, and canonical/provider
history remain. Conversation and default history views are bounded metadata;
individual reported text requires an explicit owning interactive audit read.
The default text retention is 30 days, independently of action authority.

The dot/plugin/MCP Events adapter is separate work. Historical utterance-backed
work and signed artifacts retain their provenance and recovery constraints;
the migration does not translate or automatically execute them.

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

Runtime integration validation: `scripts/docket check` passed 1,000 tests,
Ruff, and strict mypy across 160 source files. `scripts/docket compose-smoke`
passed the actual HTTP request-admission and stage/review/commit/replay path,
missing-context and cross-role rejection, late capture-gap recording, historical
restricted-ingress controls, governance restore, and the PostgreSQL rehearsal.
The latter verifies concurrent admission, exclusive foreground dispatch,
concurrent commit to one ChangeSet, immutable attribution, irreversible transcript
purge, exact interrupted invocation recovery, guarded downgrade with retained
history, and empty downgrade/re-upgrade through `20261007c3d4`.

Deterministic adapter tests inject transcript, attachment, and response archive
failures and verify foreground tools and lease completion continue. Shared-session
background tasks cannot inherit the foreground binding. Request cancellation,
corrected normalized entries, transcript-free conflict resolution, frozen relative
dates, encrypted attachment attribution, and delayed archival ingress are covered.
These checks used isolated smoke credentials and synthetic data. Live Hermes/model
and Discord delivery behavior remains an operator-present rollout check.

Deployment has not run. Before an authorized rollout, create the distinct triage
credential and reinstall the isolated profile, then use the normal backed-up
deployment path with green CI. The dot integration remains a subsequent delta.

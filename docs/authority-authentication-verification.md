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

This initial slice supplies the persistence and identity primitives. Integration
with MCP, Hermes, staging, commit, conflict resolution, optional attachments,
retention, and operational role configuration remains part of the active work.
The dot/plugin/MCP Events adapter is separate work.

Initial slice validation: `scripts/docket check` passed 984 tests, Ruff, and
strict mypy. `scripts/docket compose-smoke` passed isolated authenticated MCP,
governance restore, existing request/revision races and provider recovery,
new concurrent request admission and immutable attribution, and PostgreSQL
migration downgrade/re-upgrade. Tests used smoke credentials and synthetic data.
No production deployment or live provider operation was performed.

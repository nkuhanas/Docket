# Interaction correction verification

`ONT-DELTA-2026-09-11-INTERACTION-CORRECTION` is signed at SHA-256
`39ca596f2ff00e70cad28fe6e3750f284efde53d4db47d8490531d61ae9fd23a`.
Its private source remains under `deltas/`. The packaged artifact manifest
registers its exact command, implementation scope, and five prerequisite
specification Decisions. Registration does not establish sign-off.

September 11 read-only production verification established the exact chain:
`utt_01M293W0FNQ8NSMP6K71WQRNAQ` →
`dec_01M293W1JA9CZFRMNY3FR2A48D` →
`aud_01M293W1JBB5B6PZQXTKS7AXM0` →
`rsp_01M293W1JMYAYT29RBGDK1MK0A` (delivered).
Implementation is in progress; sign-off does not mean the amendment is deployed.

The enablement slice changes only manifest metadata and verification. It does
not activate occurrence actions, remove direct commit, change skills, migrate
drafts, or trigger calendar repair. There is no new provenance bootstrap.

## Sign-off gate

After deploying the enablement slice, send the manifest's exact `signoff_text`
through the authenticated Docket Discord surface. The existing deterministic
recognizer must capture `utt_`, record one `specification_signoff` `dec_` and
`aud_`, and deliver the durable `rsp_`. Inspect those references before marking
implementation authority available; a gateway reaction is insufficient.

The candidate requires all five exact prerequisite Decisions, including
`dec_01M1W7648P7YJZ22GRD114WSBV` for incremental assembly. A matching document
and hash under a different Decision ref cannot satisfy a prerequisite. No
production reset authority is granted. Deployment remains a separately directed
operational action.

Tests exercise real sign-off service transactions, replay, wrong hashes,
non-exact text, each substituted prerequisite, and zero canonical event,
ChangeSet, or provider Operation creation during sign-off. The tests read only
packaged metadata and synthetic test fixtures; private source files and
production state are not CI dependencies.

Local enablement verification on September 11 passed 369 tests, Ruff, and strict
mypy, including the same full gate from a clean Git archive without private
sources or runtime state. Isolated Compose smoke passed MCP checks, synthetic
governance export/restore, PostgreSQL assembly races, and migration round-trip.
Those results verify registration and the existing runtime only; they do not
constitute implementation evidence for the new UX behavior.

## Implementation gate

The private readiness design separates planned acceptance fixtures from passing
results. After ledger sign-off, deliver occurrence safeguards/instruction
isolation first, then staging/repair, deployment/provider recovery, and
diff/trace/performance work. Record passing deterministic and PostgreSQL evidence
as it exists; do not mark planned work implemented.

The central workflow is mandatory staging with optional review. Stage readiness
must distinguish saved errors from a committable revision. The final deployment
gate includes exact user outcomes, service-enforced occurrence scope, lost-response
and concurrency tests, compiler version continuity, provider duplicate prevention,
generated contracts, full checks, and isolated Compose smoke.

Live latency is measured from durable ingress to delivery of the canonical-commit
receipt; provider completion is separate. Deterministic fixtures are not evidence
of live latency. Historical calendar repair is not an automatic deployment step.

## Duplicate execution correction

An additional Operator report exposed two competing ingress paths: native
Discord delivery and Docket's deferred dispatcher. Read-only September 11
evidence found two recent utterances each with two leases. The first lease was
completed with `agent_turn_not_finalized` within seconds; a final response was
persisted minutes later, then a second lease was acquired. This establishes
erroneous re-admission, not a measured count of duplicate model generations.

The correction serializes claim admission on the immutable utterance, binds
processing callbacks to the actual dispatch event, and explicitly completes the
deferred dispatch's response lifecycle. Deterministic sign-offs have one delivery
owner. A terminal response fences re-execution even when its Discord delivery is
pending. An old completion token cannot release a newer claim or another message
under the same gateway lifetime. Genuine unfinished execution remains recoverable.

Regression coverage is in `test_deployment_continuity.py`,
`test_plugin_actor_gate.py`, and the native/deferred concurrency case in
`scripts/compose-assembly-postgres-smoke.py`. This does not replay or modify
historical requests and does not claim exactly-once delivery from Discord.

Local verification passed all 378 tests, Ruff, strict mypy, and isolated Compose
smoke, including a two-connection PostgreSQL race with exactly one execution
claim and one utterance audit. The first race run exposed an additional
deterministic-audit insertion race; serializing before audit creation fixed it,
and the complete smoke passed on rerun. No production deployment is implied.

## Reviewed instruction isolation

The Docket deployment mounts the entire Hermes discovery root read-only with
exactly `docket-manual-intent` and `docket-triage`. External discovery directories
are cleared by configuration synchronization. Runtime skill edits are rejected;
proposed improvements require repository review. Each gateway lifetime pins the
reviewed bundle's content hash, records it in trace context, and stops new model
dispatch/mutation calls if that bundle changes in place. This is instruction
isolation, not a substitute for the service-side semantic guards below.

After draining and backing up, deployment moves runtime-authored skills into
`.runtime/hermes/protocol-quarantine/skills-<random-id>` before starting the new
workers. Original files are retained, not deleted or translated. The quarantine
is outside skill retrieval. Repeating the operation on an empty root is a no-op;
redirected roots are rejected. This step has not been run against production.

Tests cover blocked skill tools, allowlist drift, bundle changes, config sync,
read-only mount configuration, and recoverable/idempotent quarantine ordering.
An isolated network-disabled container of the pinned Hermes image, without
credentials or production runtime volumes, discovered exactly the two reviewed
skills and rejected a write to its skill root with a read-only-filesystem error.

Local verification passed 386 tests, Ruff, strict mypy, and isolated Compose
smoke (MCP/authentication, governance preservation, PostgreSQL concurrency, and
migration downgrade/re-upgrade). The remaining occurrence, mandatory-staging,
semantic-repair, and delivery/latency acceptance gates are still open. This slice
does not claim the full amendment implemented or production changed.

## Occurrence identity and scope enforcement

The occurrence slice introduces internal `event_occurrences` rows, identified by
canonical series ref and the original zoned start/date, not by the replacement's
current start or a Google instance ID. Cancelling one occurrence compiles an
exception on the master. Moving it adds one routed replacement; subsequent edits
reuse that replacement. Explicit whole-series cancellation includes moved
children. Existing exclusions and adjacent occurrences remain intact. A repeated
occurrence cancellation produces a committed no-op without more provider work.

The guard runs in ChangeSet preflight and the canonical event mutation handler.
Unqualified recurring-event changes are rejected even from an older direct
client. Scope must match the persisted request, and occurrence effects must match
Docket's compiled actions. Calendar summary reads expose a compact
`mutation_target` with original identity and canonical series version, including
for moved occurrences. Unbound provider rows do not become canonical authority.

`calendar_date_bindings` pins the date and timezone resolved from the captured
utterance. The gateway supplies that message ref; a relative MCP lookup without
it fails closed. Recovery reuses the stored result despite timezone or clock
changes. Google all-day `originalStartTime.date` is retained in the provider
cache alongside timed original starts.

Migration `20260911b1e0` adds these internal tables and the all-day cache field.
PostgreSQL triggers protect original identities and date bindings. There is no
public alias, historical calendar rewrite, provider call, or domain backfill in
the migration. Its downgrade removes the new bookkeeping; it is a rehearsal
operation, not a safe production rollback after new occurrence edits. Production
recovery must use the pre-migration verified backup or a reviewed forward repair.

The staged read and recurrence tests verify original identity, DST ambiguity,
already-moved edits/cancellation, duplicate cancellation, existing exclusions,
explicit whole-series cancellation, relative-date stability, and no master
retraction through an unqualified service call. Local verification passed 408
tests, Ruff, and strict mypy. Isolated Compose smoke passed, including competing
occurrence commits (one succeeds; one receives a version conflict), concurrent
relative-date capture, database trigger enforcement, governance restore, and
migration downgrade/re-upgrade. Large scoped summaries paginate within the output
budget without losing the remaining rows. Mandatory
staging, exact repair, delivery recovery, and latency gates remain open.

## Staged-only MCP boundary

The Operator explicitly authorized the MCP API cutover. The interactive registry
now contains 23 tools: stage/review retain infrastructure-supplied utterance and
request bindings, commit exposes no model payload, and clarification persists
typed choices without canonical effects. All top-level MCP schemas reject unknown
fields. Removed direct/assembled payloads are rejected, never decoded or ignored.
Hermes scopes staging/choice schemas to requested variants and supplies execution
bindings from the captured message. Missing or stale execution context fails closed.

End-to-end MCP fixtures stage and commit without review, reject an old direct
recipe while preserving the draft, terminalize its admitted operation, and recover
the same receipt on commit retry. Clarification tests prove zero canonical changes.
The PostgreSQL smoke exposed child-before-parent flushing in semantic option
persistence; the service now flushes the immutable projection before its children
within the same transaction. The isolated ingress role can read the resulting
option but still cannot update the utterance ledger.

Local verification passed 410 tests, Ruff, and strict mypy. Isolated Compose smoke
passed the new 23-tool MCP flow, PostgreSQL races, governance restore, and migration
downgrade/re-upgrade. No production data, deployment, or historical replay changed.
The older tool count in the governing `AGENTS.md` initially remained unchanged
pending separate authorization. The Operator subsequently authorized updating
only its tool count and mutation-protocol description; the guide now matches the
23-tool, staged-only API. Authority and production-safety rules are unchanged.
The signed amendment and generated runtime contract define this API cutover.
Exact semantic repair, compact compilation, delivery recovery, diffs, and timing
acceptance remain open; this is not full-amendment completion evidence.

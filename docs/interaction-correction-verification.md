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

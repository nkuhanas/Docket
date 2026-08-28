# Ontology rollout verification

This is the operational verification companion for the ledger-signed Docket
architecture frozen at SHA-256
`3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1`.
It records implementation evidence; it does not amend the frozen ontology.

## Authority

| Evidence | Public reference |
| --- | --- |
| Authenticated architecture sign-off | `utt_01M13MANKG7HJE2WHRPWRMT528` |
| Ledger-backed `specification_signoff` Decision | `dec_01M13MANM19BX22EW8QC8AH9DT` |
| Signed document | `ONT-DELTA-2026-08-27` |
| Historical artifact with no authority | `ONT-DELTA-2026-08-26` |

The readiness record is
[`docket-ontology-readiness-status-08-27-2026.yaml`](../deltas/docket-ontology-readiness-status-08-27-2026.yaml).
No approval or implementation authority is inferred from ordinary design chat.

## Signed case-resolution amendment

`ONT-DELTA-2026-08-28-CASE-RESOLUTION` is a private signed amendment frozen
at SHA-256
`058788ec6728565b51bbce3e80d51146c52fec0c0364f7599e3877f97d964a05`.

The Operator authorized only the manifest-bound amendment-signoff bootstrap in
authenticated utterance `utt_01M157G81T7FV6A4V8RQD54Z6G`. That authority is
limited to the packaged eligible-artifact manifest, exact gateway recognition,
the generalized internal sign-off service, and the tests/runtime wiring needed
to create a later ledger-backed Decision. It does not authorize revision-ref,
CaseItem, case-resolution, ContextPacket, triage, or tool-outcome behavior.

The packaged manifest binds each eligible specification to one exact document
reference, hash, sign-off text, prerequisite Decision, implementation scope,
and—where required—bootstrap utterance evidence. Unknown or mismatched targets
fail closed. The August 27 sign-off remains unchanged and readable.

After that bootstrap was deployed, the Operator issued final authenticated
sign-off as `utt_01M1587SDFD32NX3VHKB19YRB7`; Docket created ledger-backed
`specification_signoff` Decision `dec_01M1587SE1JX3BVQ1QZBQKX6T7`. That Decision
authorizes only the amendment scope. It does not reopen or replace the August 27
architecture.

The implementation preserves the 22-tool interactive and four-tool triage
profiles. It introduces no direct case-mutation tool and no additional triage or
provider authority.

## Implemented persistence sequence

The migration sequence is additive through `0041`:

1. `0028`–`0029`: immutable utterances, final assembled responses, Decisions,
   tool calls, runtime/audit provenance, public refs, and inspection routes.
2. `0030`: IntentSession, IntentTurn, ChangeSet, Conflict, and operation linkage.
3. `0031`: typed registry, identity handles/bindings, affiliations,
   relationships, facts, interactions, and legacy provenance backfill.
4. `0032`: TriageRun, ContextPacket, AttentionCase/revisions/items, brief linkage,
   and exact reply bindings.
5. `0033`: structured Preferences, first-class CalendarLanes, and routing
   Decisions.
6. `0034`: provenance-complete CanonicalEvents and ChangeSet-authorized provider
   intent execution.
7. `0035`: AgentResponse projection bindings and response/no-response linkage
   back to durable IntentTurns.
8. `0036`: explicit `complete` versus `legacy_preledger` provider-operation
   provenance status, with complete operations requiring their originating
   ChangeSet and canonical/basis references.
9. `0037`: stable `src_` public references for provider Account identities so
   compatibility reads remain chainable without exposing internal UUIDs.
10. `0038`: PostgreSQL provenance guards compare immutable JSON fields through
    JSONB, allowing the permitted IntentTurn finalization, AgentResponse
    delivery-state update, and Conflict resolution paths without weakening the
    semantic immutability checks. The isolated Compose smoke executes the
    authenticated tool-call-to-final-response lifecycle on PostgreSQL.
11. `0039`: `sender_identity_emails` stores time-scoped operator-authorized
    associations from agent-facing sender-label handles to exact email handles.
    Exact Gmail addresses remain the deterministic match key; the label groups
    addresses and owns structured Preference policy without implying a Person
    or Organization.
12. `0040`: AttentionCase revisions receive typed `caserev_` identities with
    durable legacy `case_` aliases; stored reply/projection bindings migrate to
    the typed ref. CaseItems gain explicit required/supporting roles,
    `legacy_unspecified` migration honesty, and `not_pursued` closure state.
13. `0041`: ToolInvocations retain bounded result disposition while Discord MCP
    traces separately represent transport and reconciled durable domain outcome.

Legacy event and lane backfills are labeled `legacy_preledger` and carry a
typed external provenance source. New canonical objects are `complete` and
carry `basis_refs`, Decision/source refs, and `created_by_changeset_ref`.

## Authority and profile verification

The interactive profile exposes 22 contract entries: 20 bounded current/legacy
reads and exactly two mutations:

```text
docket_commit_changeset
docket_resolve_conflict
```

The triage profile exposes exactly:

```text
docket_get_triage_context
docket_submit_triage_analysis
docket_get_triage_case
docket_apply_existing_suppression
```

`docket_get_triage_case` is the only explicitly shared read. The interactive
contract is below 24 KiB and the triage contract is below 12 KiB. Every session
injection carries the repository contract version/hash/profile, and tool traces
carry the same values.

The MCP boundary creates `call_` after caller/profile authentication and before
schema/authority validation. Default output is null-omitted and minified,
normalizes list envelopes, and is bounded by serialized UTF-8 bytes to 16 KiB;
explicit audit view is bounded to 64 KiB. Transport truncation is not used.

## Behavioral verification

Automated acceptance is organized in these suites:

| Area | Test evidence |
| --- | --- |
| Utterance/response/tool/audit provenance | `tests/integration/test_provenance_bootstrap.py`, `test_mcp_traces.py`, `test_history.py` |
| Intent, statements, conflicts, ChangeSets | `tests/integration/test_authority_model.py` |
| Typed registry/entity resolution/graph reads | `tests/integration/test_typed_registry.py` |
| AttentionCase, cadence, suppression, reply binding | `tests/integration/test_attention_intelligence.py` |
| Preferences, sender-email associations, lanes, precedence | `tests/integration/test_preferences_and_lanes.py` |
| Canonical event, route, provider operation, uncertain outcome | `tests/integration/test_canonical_event_changesets.py` |
| Exact tool/profile/contract parity | `tests/integration/test_mcp_contract.py`, `tests/unit/test_release_script_contract.py` |
| Output byte envelope | `tests/integration/test_mcp_output_envelope.py` |
| Migration upgrade/downgrade | `tests/integration/test_migrations.py` plus the PostgreSQL rehearsal below |
| Plugin actor/source/response gates | `tests/adversarial/test_plugin_actor_gate.py` |

### Case-resolution amendment traceability

| Requirement | Automated evidence |
| --- | --- |
| `ONT-CASE-REQ-0001` | `test_case_resolution_migration_types_revision_aliases_and_preserves_bindings`, `test_legacy_case_revision_alias_resolves_to_canonical_typed_ref` |
| `ONT-CASE-REQ-0002` | `test_interactive_profile_exposes_only_reads_and_changeset_authority` |
| `ONT-CASE-REQ-0003` | `test_stale_case_revision_returns_compact_current_binding_without_mutation`, `test_case_reply_bootstraps_exact_revision_bound_intent_after_projection` |
| `ONT-CASE-REQ-0004` | `test_new_attention_case_requires_an_explicit_required_item`, migration checks in `test_initial_migration_upgrades_and_downgrades` |
| `ONT-CASE-REQ-0005` | `test_partial_resolution_keeps_omitted_required_item_open_with_one_followup` |
| `ONT-CASE-REQ-0006` | `test_already_applied_resolves_required_item_and_preserves_semantic_knowledge`, `test_explicit_event_rejection_does_not_create_event_or_operation`, `test_legacy_unspecified_item_blocks_terminal_case_closure` |
| `ONT-CASE-REQ-0007`–`0008` | `test_already_applied_resolves_required_item_and_preserves_semantic_knowledge` |
| `ONT-TRACE-REQ-0001`–`0002` | `test_mcp_trace_reconciles_authoritative_rejection_and_runtime_failure`, `test_mcp_trace_is_monotonic_redacted_and_projected` |
| `ONT-TRACE-REQ-0003` | `test_docket_mcp_hooks_emit_only_bounded_trace_metadata` |
| `ONT-GOV-REQ-0001`–`0003` | `test_manifest_bound_amendment_signoff_requires_bootstrap_and_base_signoff`, `test_amendment_signoff_forwards_exact_binding_only`, `test_exact_final_signoff_is_recorded_before_model_dispatch` |

Current source-gate result on 2026-08-28:

```text
pytest: 379 passed
ruff: all checks passed
mypy: no issues in 124 source files
git diff --check: clean
isolated Compose smoke: passed
```

## Named operational procedures

These procedures are explicit evidence targets for traceability rows. A run is
valid only when its observed refs, timestamps, schema head, contract hashes,
and outcome are appended to this document.

### ONT-OPS-UTTERANCE-FAIL-CLOSED-0001

Temporarily make the authenticated Hermes-to-Docket internal endpoint
unavailable in a disposable environment. Send one authorized Docket-chat
message and verify Hermes rejects the turn before model or mutation-tool
execution. Restore the endpoint, replay the same Discord message, and verify
exactly one `utt_` is created.

### ONT-OPS-DISCORD-OUTAGE-0001

Disable Discord delivery while leaving PostgreSQL and the outbox worker
running. Create one replyable projection and one final `rsp_`; verify canonical,
case, session, response, and outbox rows remain durable with failed/pending
delivery state. Restore Discord and verify retry reuses the projection and
response identity.

### ONT-OPS-TRIAGE-CAPABILITY-AUDIT-0001

Discover the authenticated restricted triage endpoint and compare it byte-for-
byte with the four-tool repository contract. Run a malicious-source fixture and
verify no Entity, Preference, CalendarLane, CanonicalEvent, ChangeSet, Approval,
or provider Operation is created.

### ONT-OPS-CONTRACT-TRACE-0001

Start one fresh authorized interactive session and one isolated triage run.
Verify each injected prompt and resulting trace contain the exact repository
contract version/hash/profile, the interactive surface contains 22 tools, and
the triage surface contains four tools.

### ONT-OPS-RETENTION-INSPECTION-0001

Run retention against a disposable production clone containing expired Gmail
metadata, an old authenticated `utt_`, semantic AuditEvents, and a ToolInvocation
with argument hashes. Verify eligible Gmail metadata is removed, while the
utterance and semantic provenance remain and no prohibited raw payload appears
in the ToolInvocation.

### ONT-OPS-RECONCILIATION-DRILL-0001

Use the fake or sandbox provider to return an unknown-after-transmission result.
Verify the committed `chg_` and canonical target remain effective, the `op_`
enters `reconciliation_required`, and reconciliation—not blind execution
retry—determines the terminal provider result.

## PostgreSQL clone rehearsal

On 2026-08-28 a disposable PostgreSQL 16.9 container received a direct custom
format clone of the live `0031` database. No live writes were performed.

Verified sequence:

```text
live clone at 0031
  -> upgrade 0032
  -> upgrade 0033
  -> upgrade 0034
  -> verify 11 CanonicalEvent and 6 CalendarLane provenance backfills
  -> verify provenance_sources append-only trigger
  -> downgrade 0034 -> 0031
  -> verify new tables/columns removed and trigger retained
  -> upgrade 0031 -> 0034 again
  -> verify backfills and trigger again
```

The first rehearsal exposed that migration-owned provenance rows could not be
removed while the PostgreSQL append-only trigger was active. The 0033/0034
downgrades now drop and recreate only that trigger around their own deletion;
the entire rehearsal then passed. The disposable database was removed.

A final rehearsal cloned production at `0034`, upgraded through `0035`, `0036`,
and `0037`, downgraded back to `0034`, and upgraded to `0037` again. It verified
one existing AgentResponse projection binding, 71 operations explicitly labeled
`legacy_preledger`, one unique valid Account `src_`, zero invalid complete
operations, and preservation of the AgentResponse semantic-immutability trigger.
The clone was removed after the second upgrade passed.

## Provider safety

Canonical state and provider intents commit in one PostgreSQL transaction.
Provider execution occurs afterward. A new lane operation is the predecessor of
an event operation targeting that unprovisioned lane. Provider uncertainty puts
the `op_` into `reconciliation_required`; it does not roll back the committed
`chg_`, event, registry, or lane.

New interactive workflows create no Approval rows. Legacy Approval storage and
internal handlers remain readable for drain safety. The pre-deployment live
check found zero pending Approval rows.

## Deployment evidence

Production rollout completed on 2026-08-28. Immediately before the final
migration, Docket had zero active or reconciliation-required operations, zero
pending outbox rows, and zero pending Approvals. The encrypted backup
`backups/docket-2026-08-28-20260828T104021Z.dump.age` is 3,013,935 bytes, has
ciphertext SHA-256
`c39a09dfce20e9a266fd4eaf4397ac9e54a542dd8a3ac211af9c8e8e1c94d94f`, and
was decrypted and restored successfully into disposable PostgreSQL at schema
`0034`. A second immediate custom-format backup is retained at
`backups/docket-20260828T104059Z-d66aefad5dd9-pre-0037.dump`.

The prior image is retained as
`docket-docket:rollback-ontology-before-0037-20260828T104059Z`. Production runs
image `sha256:e17233ef8fe2072b760b052c48eb1a52eed3ab6de1762068d609ec256dcb82c6`
with revision label
`d66aefad5dd9d6e9dad9135181ddd6b6b860acb3+ontology-proof-0037-working-tree`,
Alembic `0037`, and Hermes plugin `0.20.0`.

Live MCP discovery returned exactly 22 interactive tools and four restricted
triage tools. Runtime ToolInvocations recorded the v5 interactive contract hash
`fa36dfa72a6f5580409f9de89d2b86b449037446ab4c64c88c223b8cf24d6ec5`
and triage hash
`b38d1d01571c25e4aea3faaecb0522c8d44d140cbc9f67fe33ba51b37ed435ca`.
Evidence includes successful provider-reference read
`call_01M13ZFZ1YYW96J8WHSSYWR41R` and boundary validation rejection
`call_01M13ZKV3AN5KKWA8MM399WYPX`. The live provider result was 3,573 serialized
UTF-8 bytes, returned six `lane_` refs and seven `src_` refs, contained no UUID
values, and returned `isError=false`.

The first live protocol probe exposed that result compaction had retained the
text projection but dropped FastMCP's structured-output tuple. The client
therefore rejected otherwise successful reads after execution. The boundary
now preserves both compact text and structured content; the full source gate
was rerun, the fixed image was redeployed, and the bounded live read above
returned `isError=false` with a 673-byte structured result. The probes did not
mutate canonical or provider state.

Post-migration inspection found all 11 existing CanonicalEvents and all six
CalendarLanes carrying valid public references and non-empty provenance basis,
one Account carrying a unique valid `src_`, all 71 pre-ledger operations
explicitly labeled `legacy_preledger`, and zero invalid complete operations.
The existing final AgentResponse has one complete projection binding. Docket
and Hermes were healthy, Discord connected with the exact interactive registry,
and the isolated triage cron remained active. Its post-deploy execution
`2795ef6fccb746868c8ead3978e381e9` completed successfully and persisted bounded
v5 triage call `call_01M13ZN11VX46757G7N219XED1`. Active operations, pending
outbox rows, and pending Approvals remained zero.

### Sender identity association follow-up

The exact-email sender association and policy correction path deployed from
revision `5dfc73c4b901b162c2ebc7eb50c05eafdbb53812` after GitHub Actions run
`33197071788` passed both required jobs. The supported deployment created
`backups/docket-20260828T175913Z-5dfc73c4b901.dump` and retained
`docket-docket:rollback-20260828T175913Z`.

Post-deploy inspection verified Alembic `0039`, the
`sender_identity_emails` table, Hermes plugin `0.20.2`, and zero active
operations/pending outbox rows. The preexisting Mustang Shop sender-label
handle and Preference remained unchanged at version 1: the handle has zero
active email associations and the Preference still has an empty `policy_json`.
This is intentional provenance preservation. It is not effective suppression
until a new authenticated Operator correction associates the exact email and
sets the executable disposition through one ChangeSet.

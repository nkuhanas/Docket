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

## Current clean tracked-context implementation

The signed tracked-context amendment is frozen at SHA-256
`830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48`.
Its ledger authority is `utt_01M18D6GT2MXB80229HAEPZ2KB` ->
`dec_01M18DYEYJVVJ7TW5VQQBCA6NC` ->
`aud_01M18DYEYK9Y353AK546980XBK`, with final response
`rsp_01M18DYEZ3DR3JVMJMH47CJQ0J`.

The current repository implementation is a clean break:

* `item_` means Item and `citem_` means CaseItem;
* the public namespace has one meaning per prefix and no retired-prefix aliases;
* Record, RecordSource, Action, Approval, QueueItem, and obsolete schema/tool
  contracts are absent from the active model;
* one clean Alembic baseline replaces pre-cutover migration history;
* the interactive profile has 19 tools and the triage profile has four;
* Item, Task, TemporalBinding, Event, ReminderPlan, and AttentionCase retain
  distinct semantics;
* Event and Time calendar writes are compiler-owned provider operations created
  with canonical state in one ChangeSet transaction;
* attachments are provenance-bearing, encrypted according to retention policy,
  and imports bind exact source fragments;
* ToolInvocation outcome is expressed only through `transport_state`,
  `domain_state`, and `result_disposition`;
* ExecutionLease completion uses an opaque internal token and has no public
  reference namespace.

All 68 normative tracked-context clauses are `implemented_verified` in
[`docket-tracked-context-traceability-08-29-2026.csv`](../deltas/docket-tracked-context-traceability-08-29-2026.csv).
CI rejects planned-only rows, missing test functions, and missing cited
implementation paths.

This implementation has not, by virtue of repository completion, performed the
production destructive reset or deployment. Those remain separately authorized
external actions. The active operational contract is
[Operations runbook](operations-runbook.md).

## Historical pre-clean-cutover evidence

The remaining sections preserve the authority, deployment, and incident record
that led to the clean tracked-context implementation. References below to 22
tools, additive migrations, legacy rows, Approvals, Records, old prefixes, or
compatibility behavior describe the former runtime only. They are not supported
recovery procedures or requirements for the clean model.

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

## Signed interactive-continuity amendment

`ONT-DELTA-2026-08-28-INTERACTIVE-CONTINUITY` is a private signed amendment
frozen at SHA-256
`972784149dd2a219d027684a76f04fac37d8147e9656a3ff06326d883fd06579`.

The first exact Discord sign-off attempt was durably captured as
`utt_01M15DNA8XAKVDANT222F26HD0`, but the then-deployed eligible-artifact
manifest did not yet contain the frozen amendment. Docket correctly created no
Decision, although Hermes exposed the rejection only as a gateway reaction.

Revision `9278391ef82a1b70fa9a422f83f2544f2f276ff3` registered the exact artifact
against the existing August 27 ledger prerequisite and changed plugin `0.20.7`
to provide bounded trusted success/rejection context to Hermes. It does not
implement the amendment's persisted-option, authority-retry, mutation-schema,
gateway-lifetime, stable-ingress, or drain-barrier behavior.

After GitHub Actions run `33223214400` passed both required jobs, the supported
deployment created
`backups/docket-20260829T002503Z-9278391ef82a.dump`, retained
`docket-docket:rollback-20260829T002503Z`, and installed image
`sha256:58344f3999b14cd875a8e75441d7eee9e4e0af44a73304c9188b76473cc87cfc`.

The normal Docket sign-off service then reprocessed the same immutable
utterance; it created ledger-backed `specification_signoff` Decision
`dec_01M15EHKNXVKRBM7MZ3FN39X3E` and AuditEvent
`aud_01M15EHKNZR8EPCA1P306WMK6F`. The Decision has exactly that `utt_` as its
basis, names the frozen hash, grants `architecture_authority = true`, and limits
implementation authority to
`interactive_authority_continuity_and_deployment_drain_amendment`. A second
service invocation returned `replayed_request`; exactly one Decision exists.

The accepted sign-off initially received only the gateway acknowledgement
reaction: the Decision committed, but no final `AgentResponse` was assembled.
The corrective runtime now persists and projects the deterministic sign-off
confirmation directly after the Decision result, without depending on a later
model-turn completion callback. The same pass also added official `call_`
tracing for deterministic semantic-option execution, exact retry continuity,
intervening identity-binding Conflict handling, immutable safe-rebase evidence,
and disposition-first tool activity rendering.

Post-deploy verification found Docket healthy at the expected source revision,
Hermes running plugin `0.20.7`, no active provider operations, no pending outbox
delivery, and no bounded post-deploy Docket/Hermes error. The amendment is now
implementation-authoritative within its signed scope.

The substantive implementation is deployed through
`a7a16f8b655c6d84e8df41f4875d85fa58d586cf`, split across persistence, typed
mutation schemas, persisted semantic options, tool dispositions, gateway
leases, execution drains, stable ingress, quiesced ingress handoff, and
exact-scope retry commits. GitHub Actions run `33231445973` passed both required
jobs for that exact revision.

The supported deployment completed at `2026-08-29T03:30Z`, created
`backups/docket-20260829T032958Z-a7a16f8b655c.dump`, retained
`docket-docket:rollback-20260829T032951Z`, installed image
`sha256:5c61af973ef6596f2c5a8ce764d6b1be3e87305445c2ee2d9eacf55219985fab`,
and left Alembic at `0042`. The deployment-stable Discord ingress remained
healthy while Docket and Hermes restarted.

One earlier supported attempt at revision `76e5f8d` failed closed when two
simultaneous plugin registrations raced while allocating a gateway generation.
The deploy script released its drain barrier without killing active work. The
server now serializes registration replay, fencing, and generation allocation
with a PostgreSQL transaction-scoped advisory lock; the isolated Compose smoke
issues two simultaneous registrations and requires one `created` plus one
`replayed_request` result for the same `gwy_`.

Post-deploy inspection verified plugin `0.22.2`, one active gateway lifetime,
a reachable projection listener, six consecutive successful heartbeat updates,
zero executing provider operations, and zero in-flight outbox deliveries. The
latest deployment barrier is `released`; bounded Docket/Hermes logs contain no
gateway fencing, uniqueness, traceback, or error entry after the successful
deployment.

## Frozen tracked-context sign-off enablement

`ONT-DELTA-2026-08-29-TRACKED-CONTEXT` is frozen at SHA-256
`830c33c9d78485a6a6a8f872b6dfad996869f8a7eaea9a5f7d39d52e9357cf48`.
This repository revision packages only the manifest-bound recognition needed
for the Operator to sign that exact artifact through the existing ledger.

Manifest schema version 2 binds the candidate to all three exact prerequisite
Decisions:

```text
dec_01M13MANM19BX22EW8QC8AH9DT  August 27 ontology
dec_01M1587SE1JX3BVQ1QZBQKX6T7  August 28 case resolution
dec_01M15EHKNXVKRBM7MZ3FN39X3E  August 28 interactive continuity
```

The validator requires each public Decision ref together with its decision
kind, document ref, artifact hash, and architecture-authority value. The new
sign-off Decision will record the complete prerequisite list, the exact bounded
implementation scope, and `production_reset_authority=false`. Plugin `0.22.3`
will project that production reset and deployment remain unauthorized.

This enablement introduces no migration, clean public-ref prefix, Item/Task/
TemporalBinding schema, triage change, provider behavior, reset implementation,
or model-facing mutation. Amendment sign-off and all substantive readiness/
implementation work remain pending.

## Tracked-context readiness closure

The Operator subsequently signed the tracked-context artifact through the
trusted ledger as `utt_01M18D6GT2MXB80229HAEPZ2KB`, producing Decision
`dec_01M18DYEYJVVJ7TW5VQQBCA6NC`, AuditEvent
`aud_01M18DYEYK9Y353AK546980XBK`, and final response
`rsp_01M18DYEZ3DR3JVMJMH47CJQ0J`. All eight implementation-start gates are now
resolved in
[`docket-tracked-context-readiness-status-08-29-2026.yaml`](../deltas/docket-tracked-context-readiness-status-08-29-2026.yaml).

Revision `18356da68dd76a14cedf675a71a35704d6121d00` added the bounded offline
rehearsal. A read-only production snapshot restored at schema `0042` with the
exact live image, then produced a 72-row governance closure with all four
artifact sign-offs intact and zero unresolved refs. The private provider
manifest covers 73 Operations, 43 OperationItems, 77 ExecutionAttempts, eight
bindings, 28 links, eight affected events, six lanes, and 36 exact external
targets. Every target disposition is `leave_external_unmanaged`; no running or
uncertain execution remains.

The clean rehearsal restores the governance closure and hashed `acct_`
ProviderAccounts through the repository's actual clean Alembic baseline—not a
reduced test-only DDL facsimile—and verifies every current model table plus the
head revision while excluding every enumerated obsolete table. An
encrypted attachment fixture survived custom-format backup/restore and
decrypted to its original SHA-256. Exact dumps, utterances, labels, account
addresses, and provider IDs remain mode-`0600` in ignored backup storage. The
tracked, non-sensitive hashes and counts are in
[`docket-tracked-context-rehearsal-evidence-08-29-2026.yaml`](../deltas/docket-tracked-context-rehearsal-evidence-08-29-2026.yaml).

This closes implementation readiness only. No clean-model migration, production
deployment, destructive reset, production row deletion, or provider mutation
occurred. Production reset remains blocked on the later manifest-, backup-, and
deployment-revision-bound authenticated authorization required by G.3.

The production-reset entrypoint additionally binds the sealed backup, closure,
provider manifest, rehearsal evidence, and exact Git revision; verifies the
ledger-backed reset Decision before and after drain; recomputes only the
governance extension caused by that authority turn; and verifies the authority
chain again inside the clean database before any old database is removed. A
successful cutover appends `production_reset.completed` as an `aud_` whose basis
is the exact reset authorization chain. This machinery does not itself grant
reset or deployment authority.

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
14. `0042`: immutable persisted semantic options, semantic request/attempt
    lineages, gateway leases, execution leases, drain barriers, and deferred
    ingress provide authority continuity across validation failure, restart,
    and deployment. The migration is additive so the prior ingress writer
    remains schema-compatible through normal rollout.

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

### Interactive-continuity amendment traceability

| Requirement | Automated evidence |
| --- | --- |
| `ONT-CONT-REQ-0001` | `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-REQ-0002` | deterministic visible-scope assertions in `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-REQ-0003` | `test_model_facing_mutations_reject_loose_or_unsupported_shapes`, `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-REQ-0004` | immutable selection-ledger assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` and `test_old_option_safe_rebase_preserves_immutable_selection_evidence` |
| `ONT-CONT-REQ-0005` | `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-REQ-0006` | replay assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-REQ-0007` | `test_selection_validation_failure_preserves_authority_without_duplicate_attempt`, `test_repaired_runtime_retries_same_semantic_request_without_new_authority` |
| `ONT-CONT-REQ-0008` | `test_intervening_identity_binding_opens_conflict_without_partial_commit`, `test_old_option_safe_rebase_preserves_immutable_selection_evidence` |
| `ONT-CONT-REQ-0009` | `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-REQ-0010` | `test_intervening_identity_binding_opens_conflict_without_partial_commit`, `test_dependency_cycle_blocks_before_any_handler_runs` |
| `ONT-CONT-REQ-0011` | `test_model_facing_mutations_reject_loose_or_unsupported_shapes` plus schema assertions in `test_interactive_profile_exposes_only_reads_and_changeset_authority` |
| `ONT-CONT-REQ-0012` | typed identity-binding and `operator_selection` assertions in `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-REQ-0013` | `test_dependency_cycle_blocks_before_any_handler_runs`, `test_rich_registry_graph_commits_atomically_with_create_references` |
| `ONT-CONT-REQ-0014` | generated schema/example assertions in `test_interactive_profile_exposes_only_reads_and_changeset_authority` and `test_generated_tool_contracts_have_exact_profile_parity_and_hashes` |
| `ONT-CONT-REQ-0015` | first-call/no-rejection assertions in `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-REQ-0016` | `test_docket_discord_profile_has_no_mutation_escape_capabilities`, `test_internal_api_and_mcp_require_distinct_tokens` |
| `ONT-CONT-REQ-0017` | blocked-response and preserved-authority assertions in `test_selection_validation_failure_preserves_authority_without_duplicate_attempt` |
| `ONT-CONT-REQ-0018` | `test_terminal_trace_preserves_needs_clarification_disposition`, `test_mcp_trace_projects_semantic_disposition_before_transport_details`, ToolInvocation assertions in `test_provenance_bootstrap.py` |
| `ONT-CONT-REQ-0019` | `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes` |
| `ONT-CONT-REQ-0020` | `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes` |
| `ONT-CONT-REQ-0021` | `test_drain_waits_only_for_prebarrier_execution_leases`, deployment-script contract checks |
| `ONT-CONT-REQ-0022` | `test_drain_waits_only_for_prebarrier_execution_leases`, `test_deploy_drains_execution_but_preserves_queued_durable_work` |
| `ONT-CONT-REQ-0023` | `test_drain_timeout_aborts_without_cancelling_active_work` |
| `ONT-CONT-REQ-0024` | `test_ingress_handoff_quiesces_and_regenerates_exact_semantic_options` |
| `ONT-CONT-REQ-0025` | `test_old_option_safe_rebase_preserves_immutable_selection_evidence` |
| `ONT-CONT-REQ-0026` | `test_selection_validation_failure_preserves_authority_without_duplicate_attempt`, `test_repaired_runtime_retries_same_semantic_request_without_new_authority` |
| `ONT-CONT-REQ-0027` | `test_repaired_runtime_retries_same_semantic_request_without_new_authority` |
| `ONT-CONT-REQ-0028` | `test_conflict_resolution_commits_through_one_immutable_changeset`, generic-schema exclusion in `test_interactive_profile_exposes_only_reads_and_changeset_authority` |
| `ONT-CONT-REQ-0029` | `test_authenticated_message_is_captured_and_deferred_during_drain`, `test_stable_ingress_selection_persists_before_worker_execution` |
| `ONT-CONT-REQ-0030` | continuity migration assertions in `test_initial_migration_upgrades_and_downgrades` and restricted-role Compose smoke |
| `ONT-CONT-REQ-0031` | `test_ingress_handoff_quiesces_and_regenerates_exact_semantic_options`, ingress-deploy script contract checks |
| `ONT-CONT-REQ-0032` | exact symbolic substitution assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-REQ-0033` | `sreq_` and attempt-lineage assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` and `test_repaired_runtime_retries_same_semantic_request_without_new_authority` |
| `ONT-CONT-REQ-0034` | `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes`, gateway lease migration assertions in `test_initial_migration_upgrades_and_downgrades` |

Acceptance coverage is also explicit:

| Acceptance | Automated evidence |
| --- | --- |
| `ONT-CONT-ACC-0001` | `test_one_click_cal_poly_resolution_commits_one_complete_changeset` |
| `ONT-CONT-ACC-0002` | replay assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-ACC-0003` | `test_selection_validation_failure_preserves_authority_without_duplicate_attempt` |
| `ONT-CONT-ACC-0004` | `test_intervening_identity_binding_opens_conflict_without_partial_commit` |
| `ONT-CONT-ACC-0005` | `test_intervening_identity_binding_opens_conflict_without_partial_commit`, `test_dependency_cycle_blocks_before_any_handler_runs` |
| `ONT-CONT-ACC-0006` | `test_terminal_trace_preserves_needs_clarification_disposition`, `test_mcp_trace_projects_semantic_disposition_before_transport_details` |
| `ONT-CONT-ACC-0007` | `test_docket_discord_profile_has_no_mutation_escape_capabilities`, `test_internal_api_and_mcp_require_distinct_tokens` |
| `ONT-CONT-ACC-0008` | `test_drain_waits_only_for_prebarrier_execution_leases`, deployment-script contract checks |
| `ONT-CONT-ACC-0009` | `test_deploy_drains_execution_but_preserves_queued_durable_work` |
| `ONT-CONT-ACC-0010` | terminal-outcome branch in `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes` |
| `ONT-CONT-ACC-0011` | unknown-outcome branch in `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes` |
| `ONT-CONT-ACC-0012` | `test_ingress_handoff_quiesces_and_regenerates_exact_semantic_options`, `test_old_option_safe_rebase_preserves_immutable_selection_evidence` |
| `ONT-CONT-ACC-0013` | `test_repaired_runtime_retries_same_semantic_request_without_new_authority` |
| `ONT-CONT-ACC-0014` | `test_authenticated_message_is_captured_and_deferred_during_drain`, `test_stable_ingress_captures_typed_message_without_domain_authority`, `test_stable_ingress_selection_persists_before_worker_execution` |
| `ONT-CONT-ACC-0015` | `test_conflict_resolution_commits_through_one_immutable_changeset`, generic-schema exclusion in `test_interactive_profile_exposes_only_reads_and_changeset_authority` |
| `ONT-CONT-ACC-0016` | `test_old_option_safe_rebase_preserves_immutable_selection_evidence` |
| `ONT-CONT-ACC-0017` | symbolic substitution assertions in `test_persisted_selection_compiles_once_and_preserves_exact_authority` |
| `ONT-CONT-ACC-0018` | `test_expired_gateway_reconciles_terminal_and_unknown_call_outcomes` |

Current source-gate result on 2026-08-28:

```text
pytest: 410 passed
ruff: all checks passed
mypy: no issues in 136 source files
git diff --check: clean
isolated PostgreSQL Compose smoke: passed
```

The Compose smoke upgraded PostgreSQL through `0042`, executed authenticated
MCP/provenance response finalization, concurrently replayed one gateway
registration without allocating a duplicate lease generation, and connected
with the restricted `docket_ingress` role. That role read its bounded
persisted-option table and PostgreSQL rejected an attempted `UPDATE
operator_utterances`. The generated v10 interactive contract hash is
`5169d64f25a55d1d382aceca0fd8f13344ab062355bedcc0ad3c03ebb480748f`;
the v10 restricted-triage hash is
`d2669b57df80249a291796943393fa546f35462665b140566ba14961d0dca243`.

GitHub Actions run `33233296737` passed both required jobs for corrective
revision `30890dbedac6bdc25ae09dbcd5aae14cf52ac831`. The supported deployment
used drain `drain_01M15W58GHN3ECYPT002NP2TFC`, created backup
`backups/docket-20260829T042403Z-30890dbedac6.dump`, retained rollback image
`docket-docket:rollback-20260829T042357Z`, and installed runtime image
`sha256:898c5a29a7dcaeeeddba7b3121002c99e3c11da5261d90e7c47cdeb36fc23cd3`.
Post-deploy verification found migration `0042`, one active heartbeating
gateway lifetime, zero active execution leases, zero running ToolInvocations,
zero in-flight outbox deliveries, and no bounded Docket/Hermes/ingress error.

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

### Case-resolution amendment deployment

The substantive amendment deployed from revision
`12c36baed96327e3ace2e50b8bad66d276a7cf7f` after GitHub Actions run
`33220038019` passed both required jobs. The supported deployment created
`backups/docket-20260828T232121Z-12c36baed963.dump`, retained
`docket-docket:rollback-20260828T232121Z`, and installed image
`sha256:147328e26c6814d63c7cd6bdb9dfd7f59527e01c2ef04f98e57d0ec251965cd6`.

Post-deploy inspection verified Alembic `0041`, Hermes plugin `0.20.6`, 22
interactive tools, four restricted triage tools, interactive contract hash
`d68e23466fea764b51f42ba37b595ff3174f50cff90a8786f09900d956e5c7cc`,
and triage contract hash
`ad37a66dd61a97e7ab4080cc1d971e6fd2522edc83a03700f6733c00ca73a4e5`.

All four pre-amendment AttentionCaseRevision rows now use valid `caserev_`
identity and preserve four unique legacy aliases. Eight scalar bindings and the
existing IntentSession/AgentResponse revision lists use typed refs; no legacy
`case_` revision binding remains. All 13 pre-amendment CaseItems are honestly
`legacy_unspecified`; no role or status is invalid. No stored MCP trace retains
the legacy conflated `state` key. The signed amendment Decision remains present,
and active operations and pending outbox rows are both zero. No Docket or Hermes
error/traceback appeared in the bounded post-deploy log inspection.

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

## Tracked-context clean cutover

The signed tracked-context production reset completed on 2026-08-30 at revision
`77d399b56ea6cd6b52e5ec24ac2fa559b11204b0` after GitHub Actions run
`33347572245` passed both required jobs. The reset was bound to manifest SHA-256
`af6f7cf7cfd3710f39f5f36928dfbbf771ac4e09d0dc1332ac9c3df53f0560c9`
and sealed backup
`backups/tracked-context-readiness-20260831T012749Z/pre-reset-source.dump` at
SHA-256
`84042f9169f637fc6175a02816bc63044cc7689e5fe0ba3631becdc39a86bd69`.

The authenticated authority chain is:

```text
utt_01M1AQ06PB3B6A22JYS7ZP38AY
  -> dec_01M1AQ130PQPYPN2TPYC2BNRTP
  -> aud_01M1AQ130QA4A5JGYT2W72443N
```

The clean materialization preserved 67 governance rows, all four required
specification sign-offs, the exact reset authority, and one provider account.
It reset disposable operator-domain state rather than translating it through
legacy aliases or synthetic backfills. Production now has only the `docket`
database at clean Alembic head `2022877699cf`, 75 clean model tables plus
`alembic_version`, and zero obsolete Record, RecordSource, QueueItem, or
Approval tables. The quarantined pre-reset database was removed only after
post-deploy verification passed; the sealed backup remains offline.

The live Docket image is
`sha256:31a5c758d7268322e3bc38976d0fe252e06526ad7a58ec57883ca0fcfb583fa8`
with revision label `77d399b56ea6cd6b52e5ec24ac2fa559b11204b0`.
Docket and the stable Discord ingress are healthy, Hermes is connected with
exactly 19 interactive tools, and the isolated triage profile exposes exactly
four non-authoritative tools. Active provider execution and outbox delivery
counts were both zero after cutover. Completion is recorded durably as
`aud_01M1AQ3XN2HVEV7XXNW835N3B3` with event type
`production_reset.completed`.

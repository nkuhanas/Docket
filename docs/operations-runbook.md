# Operations runbook

This runbook describes the clean tracked-context runtime. Historical rollout
evidence is retained separately in
[Ontology rollout verification](ontology-rollout-verification.md); retired
Record, Approval, Action, QueueItem, and pre-cutover MCP flows are not runtime
recovery paths.

Never print or paste service tokens, OAuth files, attachment keys, raw Gmail
bodies, authorization headers, or unredacted retained utterances. Prefer typed
public references and bounded history views over internal UUIDs or table dumps.

## Runtime invariants

PostgreSQL is the only durable authority. The live flow is:

```text
authenticated Operator input
  -> immutable utt_ before interpretation
  -> IntentSession / statements / conflict handling
  -> one chg_ canonical transaction
  -> canonical objects + required op_ provider intents
  -> asynchronous provider execution and reconciliation
```

The interactive MCP profile exposes exactly 20 tools. Only
`docket_commit_changeset` and `docket_resolve_conflict` can mutate canonical
state. The isolated triage profile exposes four non-authoritative tools;
`docket_get_attention_case` is the sole shared read.

The principal public vocabulary is:

```text
ent_    persistent Entity       item_   bounded tracked Item
task_   operator work           time_   temporal meaning
evt_    actual occurrence       rem_    notification behavior
case_   AttentionCase           citem_  case component
utt_    Operator evidence       stm_    interpretation
chg_    atomic mutation         op_     provider effect
call_   tool invocation         aud_    semantic audit
```

Provider writes are compiler-owned. An authorized Event or Time projection
ChangeSet must create any required `op_` in the same transaction. Hermes does
not formulate provider intents and must never ask for a second “push to Google”
authorization after an already-authorized canonical mutation.

## First checks

From the repository root:

```bash
scripts/docket status
sudo docker compose ps
sudo docker compose logs --since 15m docket hermes discord-ingress
```

Keep log output bounded. For a reported tool problem, begin with its `call_`,
`trace_`, `ses_`, `chg_`, `op_`, `case_`, or `proj_` reference through the
bounded history tools or trusted internal history API. Do not start by dumping
all utterances or provider payloads.

The Docket tool-activity projection includes compact turn timing. A large
`before_first_tool_ms` indicates gateway/session/model preparation such as
compression or queueing, while `tool_execution_ms` is the sum of bounded Docket
tool calls. `outside_tool_ms` includes all non-tool model/agent time and is
diagnostic only; it is not evidence that a provider or canonical mutation ran.

Health endpoints distinguish liveness from readiness:

```bash
curl -fsS http://127.0.0.1:${DOCKET_PORT:-8080}/health/live
curl -fsS http://127.0.0.1:${DOCKET_PORT:-8080}/health/ready
```

A ready API does not prove that a provider operation succeeded or that a
Discord projection was delivered. Inspect those durable lifecycles separately.

## Local verification

The required deterministic gate is:

```bash
scripts/docket check
```

Changes to migrations, Compose, authentication, startup, MCP, tool contracts,
providers, or dependencies additionally require:

```bash
scripts/docket compose-smoke
```

The smoke stack uses `.env.example` and `secrets/smoke/`. It must never inherit
the production `.env` or production credentials.

## Tool and contract diagnosis

When Hermes appears to use the wrong schema:

1. Confirm the running Docket and Hermes image revisions.
2. Confirm the interactive profile reports 19 tools and triage reports four.
3. Compare the injected contract version, hash, and profile with the generated
   repository artifacts.
   Confirm a namespaced `mcp__docket__docket_commit_changeset` description with
   exact `mutation_types` returns the scoped Task/Time field definitions rather
   than Hermes's generic truncated description.
4. Inspect the `call_` lifecycle:
   `transport_state`, `domain_state`, and `result_disposition` are distinct.
5. Reload MCP only after the server and generated contract agree.

Do not probe hidden HTTP endpoints or use terminal access to discover a mutation
shape. The Pydantic/FastMCP schema is the structural contract; the generated
Markdown contract supplies authority, selection, side-effect, and result rules.

Every authenticated tool invocation receives one `call_`, including validation
and authority rejection. Tool logs retain hashes and bounded references, not raw
arguments or results. A conversational trace marked interrupted has
`domain_state=unknown` unless a durable terminal Docket outcome proves otherwise.
Local schema rejection is a completed transport with
`result_disposition=rejected_validation`; a trace card left at `running` for
such a rejection indicates a trace-callback contract failure, not live Docket
work.

## Operator input and response failures

If an Operator message receives only a reaction or no final response:

1. Verify that one `utt_` exists for the exact Discord message/interaction.
2. Inspect its IntentSession and ToolInvocations.
3. Determine whether a `chg_` committed before diagnosing delivery.
4. Inspect `rsp_`, `proj_`, projection delivery, and outbox state separately.
5. Reconcile a dead or cleanly replaced gateway lifetime against durable
   outcomes; never relabel a committed ChangeSet as interrupted merely because
   response delivery failed.

An input that cannot be durably captured fails closed. A generated response may
exist even when delivery failed; retry the same projection identity rather than
creating another semantic response.

## Attention and brief diagnosis

Triage may suppress under an existing Preference, create `bentry_` informational
output, or admit one `case_` for a concrete unresolved canonical consequence. An
unknown sender alone is not an attention reason.

During the active window, a new AttentionCase is queued for individual
projection. Overnight cases remain durable and appear in one morning brief. The
night brief covers daytime triage. A Discord reply binds to the exact visible
`proj_`, case or brief revision, and resumes an authenticated IntentSession.

For a noisy or stuck case, inspect:

```text
tri_ -> ctx_ -> source refs
case_ -> caserev_ -> required/supporting citem_
proj_ -> delivered Discord message
reply utt_ -> ses_ -> chg_
```

Required CaseItems need an Operator-backed terminal disposition. Supporting
items may become `not_pursued`; that is not the same as Operator rejection. A
case reply must apply the narrowest effects supported by the utterance.

Useful triage controls are:

```bash
scripts/docket gmail-status
scripts/docket gmail-triage-status
scripts/docket gmail-triage-pause
scripts/docket gmail-triage-run
scripts/docket gmail-triage-resume
```

The one-shot run requires the recurring job to be paused. Gmail evidence is
untrusted and triage cannot mutate canonical objects or providers.

## Item, Task, Time, Event, and Reminder diagnosis

Keep primitive boundaries explicit:

```text
Item     bounded thing being tracked
Task     work the Operator needs to do
Time     due/scheduled/open/expected/effective temporal meaning
Event    occurrence with scheduling/attendance semantics
Reminder notification behavior
```

A date does not create an Event. A Time calendar marker is a provider projection
of `time_`, remains distinguishable from `evt_`, and requires an explicit display
policy and CalendarLane. An Event linked to an Item must be temporally compatible
with the Time it claims to realize.

For a missing Calendar object:

1. Resolve whether the target is `evt_` or a Time marker (`time_` + `tproj_`).
2. Verify the lane and active route or projection.
3. Verify that the committing `chg_` contains a compiler-produced `op_` target.
4. Inspect Operation and ExecutionAttempt state.
5. If request transmission may have occurred, reconcile rather than retrying
   blindly.
6. Confirm the provider binding and fresh Calendar cache independently.

A Google popup ReminderPlan requires an Event provider binding or an active/
same-ChangeSet Time projection. Docket queue reminders can target Event or Time
without turning a deadline into an Event.

## Attachment evidence

An Operator attachment first creates bounded `src_` metadata and, according to
retention policy, an encrypted blob. Interpretation and mutation wait until
durable bytes are available. Attachment contents remain untrusted.

Imported Items must point to exact derived source-fragment statements. Exact
fragment correlation is idempotent, while identical bytes from distinct uploads
do not silently merge semantic Items. The safe default import scope permits
context only; Task, Event, Reminder, provider, Preference, and destructive
effects require explicit Operator scope.

Attachment download tries Discord's fresh URL before the cached proxy. A
matching replay cannot redefine terminal evidence, and a failed/rejected
capture terminates the ingress with a durable Operator response instead of
reaching interpretation or retrying indefinitely. If capture fails, verify the
terminal ingest and retention disposition without printing plaintext. The
encryption key must be retained with credential backups or retained blobs
cannot be restored.

When Hermes cannot natively consume a retained PDF, it reads the exact `src_`
through `docket_read_attachment_text`. The tool returns bounded, paginated,
untrusted text with page/character locators, fragment hashes, and the extractor
identity/version required for derived statements. It never returns attachment
bytes. A PDF without a text layer fails explicitly; OCR is not currently
advertised or inferred.

## Provider operations and reconciliation

Canonical commit and provider execution are separate outcomes. A failed or
uncertain provider call never rolls back unrelated committed canonical state.

Inspect:

```text
chg_ -> op_ -> execution attempt -> provider binding
```

Only retry when the durable state machine proves no transmitted request can be
duplicated. Unknown-after-transmission uses reconciliation. Never mark an
operation succeeded to clear a queue or make a deployment pass.

External write gates in production fail closed. Enabling a gate does not itself
authorize a new semantic effect.

## Deployment and drain

Deployment is distinct from push and requires explicit Operator direction.
Normal deployment is:

```bash
scripts/docket predeploy
scripts/docket deploy
```

`predeploy` requires a clean `main` exactly matching `origin/main`, both GitHub
CI jobs green for that SHA, production configuration, and safe durable state.
`deploy` establishes a drain barrier, lets pre-barrier execution leases finish,
captures later ingress durably for deferred processing, creates a backup,
upgrades the schema, replaces services, and verifies the result.

Queued durable operations and outbox rows survive restart; only claimed/in-flight
work blocks the drain. A drain timeout aborts without cancelling active work.

Deploying the stable Discord ingress itself uses the separately quiesced path:

```bash
scripts/docket deploy-ingress
```

Do not manually restart the gateway in the middle of an Operator turn. After an
unclean lifetime expires, reconciliation preserves any durable domain result and
marks only evidence-free conversational execution interrupted/unknown.
The same reconciliation runs when a drained deployment cleanly replaces a
gateway. If an `rsp_` or terminal `turn_` already proves execution finished, its
claimed ingress becomes completed rather than pending and is never re-executed;
only an ingress without durable terminal evidence is released for resumption.

## Clean reset boundary

The signed tracked-context amendment authorizes implementation and rehearsal of
the clean reset. It does not authorize deleting production operator-domain data,
performing provider mutations, or deploying the reset.

Read-only rehearsal is available through:

```bash
scripts/docket readiness-rehearsal
```

It snapshots production, restores the matching pre-reset image, exports the
governance closure, inventories provider effects, creates isolated clean
databases, and verifies attachment backup/restore. Private evidence remains in
mode-`0600` ignored backup storage. A successful run also writes a sealed
`production-reset-manifest.json` and prints the one exact authorization message
bound to its backup hash and the full deployment revision. Any subsequent code
commit requires a new rehearsal and manifest because the revision binding no
longer matches.

Send that byte-exact authorization through the trusted Docket/Discord path.
Before cutover, verify that it produced an immutable `utt_`, a
`production_reset_authorization` `dec_`, and its `aud_`. A checkmark or ordinary
chat response is not sufficient evidence.

The destructive command is intentionally separate from rehearsal and from the
Discord authorization:

```bash
scripts/docket production-reset \
  backups/tracked-context-readiness-YYYYMMDDTHHMMSSZ \
  --execute
```

Run it only after a separate explicit Operator direction to perform the reset
and deployment. The command requires synchronized `main`, green GitHub CI, the
exact evidence directory, the exact ledger Decision, and the matching old image.
It verifies everything before drain and again after drain; stops all database
writers; recomputes the final governance closure with the reset-authorization
chain; materializes and verifies an empty `docket_cutover_*` database; then swaps
database names. The pre-reset database and image remain quarantined only until
the clean service, ingress, Hermes registry, schema head, and authority chain
pass. They are then removed from the live cluster while the sealed custom-format
backup remains offline.

Before the final database swap, any failure drops only the explicitly named
empty cutover database and restores the pre-reset image. After the swap but
before verification completes, failure renames the quarantined pre-reset
database back to `docket`, restores the matching image, and removes the failed
clean database. If that recovery itself fails, leave PostgreSQL and application
services stopped and restore the sealed backup with the matching image; never
start either image against the other schema.

Never execute a production reset without a later exact authenticated Operator
instruction bound to the reset manifest, backup, and deployment revision. The
governance ledger, specification-signoff Decisions, sign-off audits, artifact
identities, provider/account configuration, and required authority provenance
must survive. Disposable operator-domain state receives no compatibility alias,
decoder, or synthetic backfill.

## Backup and restore

Create or confirm the encrypted backup:

```bash
scripts/docket backup
```

Verify restore into disposable PostgreSQL:

```bash
scripts/docket verify-restore BACKUP_PATH
```

An image rollback does not reverse a database migration. Use the exact migration
recovery plan and verified backup; do not retag an old image against a newer
schema and hope for compatibility.

## Credentials and external dependencies

Production credentials belong in the configured secret directory, never the
repository or logs. Reauthorize Hermes only through:

```bash
scripts/docket setup-hermes-auth --main
scripts/docket setup-hermes-auth --triage
```

Calendar and Gmail provider identity must resolve through clean `acct_`
ProviderAccounts. SearXNG is a network-private search dependency for Hermes; it
is never canonical state or provenance authority.

## Handoff checklist

Before reporting an operational change complete, record:

- exact behavior changed and public refs used for verification;
- focused tests plus `scripts/docket check`;
- `scripts/docket compose-smoke` when required;
- commits created;
- whether anything was pushed;
- whether anything was deployed;
- whether any production data or provider state changed;
- remaining reconciliation, reset, or Operator step.

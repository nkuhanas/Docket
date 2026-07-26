# Milestone 4 Gmail verification

This report covers the Gmail ingestion, semantic-triage, proposal, approval, and
mutation boundary. Gmail send and reply are absent. The production action
registry remains disabled until the operator-present mutation gate at the end of
this document passes.

## Delivered boundary

Docket owns:

* one leased `gmail:inbox` checkpoint per enabled Gmail-capable Google account;
* history pagination and bounded overlap recovery after an invalid cursor;
* atomic source staging and cursor advancement;
* immutable source versions and exact account/message/version action binding;
* expired semantic-triage claim recovery;
* semantic queue deduplication and supporting/update source attachment;
* immutable archive and mark-read proposals;
* Discord-authenticated approval and durable mutation operations;
* crash-safe unknown-outcome reconciliation; and
* redacted queue cards, operational logs, and stale-scan alerts.

The real and fake provider contracts expose only:

```text
scan_page
read_message
get_label_state
mutate_message
```

Archive removes `INBOX`; mark-read removes `UNREAD`. A pre-read makes an
already-applied effect idempotent. A transport failure or ambiguous HTTP result
after a mutation starts is an unknown outcome and must be reconciled by
refetching label state before any retry.

Email body and attachment content are returned only for a current claimed
source. Docket persists minimal headers, provider identity/version, bounded
attachment metadata, and derived triage facts; it does not persist the body,
quoted text, links, or attachment bytes.

## Restricted Hermes runtime

Semantic triage uses a named `docket-triage` Hermes profile with:

* no Discord gateway;
* no plugins;
* empty CLI and cron platform toolsets;
* no general shell, record-write, approval, operation, Calendar, or provider
  mutation tools; and
* exactly four MCP tools:

```text
docket_claim_triage_batch
docket_read_claimed_source
docket_search_related_records
docket_submit_triage_decision
```

The root Hermes cron invokes a fixed no-agent launcher every 30 minutes. That
launcher starts the pinned profile as a separate one-shot agent and delivers the
result to local logs only. The root scheduler never runs the interactive model
for this job, while the child model receives only the isolated profile. Normal
completion is `[SILENT]`. Install or repair it with:

```bash
scripts/docket setup-triage
```

The interactive Discord profile must not expose `/triage-mcp/` or any of these
four tools. The triage profile must not inherit messaging credentials or the
Docket internal approval token.

## Deployment gates

Three independent settings must all be true before a Gmail operation can run:

```text
DOCKET_EXTERNAL_WRITES_ENABLED=true
DOCKET_GMAIL_INGESTION_ENABLED=true
DOCKET_GMAIL_WRITES_ENABLED=true
```

Ingestion alone permits only scans, transient claim reads, and local triage.
With Gmail writes false, approval returns `external_writes_disabled` without
consuming the approval or creating an operation. Automated tests and Compose
smokes explicitly force both Gmail settings false and never inherit production
credentials.

The static Gmail action-registry entries were enabled only after the controlled
live gate passed. OAuth possession of `gmail.modify` does not enable runtime
authority: every provider mutation still requires the runtime write gate and a
current, operator-approved immutable action revision.

## Automated evidence

The test suite covers:

* multi-page stage/checkpoint atomicity and exact replay;
* cursor invalidation and bounded overlap recovery;
* active and expired lease behavior;
* Gmail-capable account selection;
* stale-scan alert deduplication;
* semantic deduplication and source attachment;
* malicious content proposing, but never approving or executing, an action;
* exact-message archive and mark-read execution;
* disabled production-write behavior;
* newer source-version rejection;
* unknown-after-write reconciliation;
* worker crash after provider mutation but before local success recording;
* permanent reconciliation failure without blind retry;
* redacted cards and system logs;
* isolated four-tool MCP discovery; and
* absence of Gmail send/reply tools.

Run:

```bash
scripts/docket check
scripts/docket compose-smoke
```

The repository test suite must be green before deployment. Compose smoke must
report disabled Gmail read/write gates and exactly four tools on
`/triage-mcp/`. Named operator controls now resolve exactly one
`Docket Gmail triage` job before showing status, pausing, queueing a one-shot
run, or resuming the schedule; paused jobs remain discoverable. A temporary
server-enforced source UUID allowlist can constrain an operator-present semantic
pass without trusting the model to avoid the rest of a staged backlog. Health
exposes only its count, and the final soak rejects any remaining scope.

## Controlled live gate

Keep Gmail writes disabled for the first deployment.

1. Install the isolated profile with `scripts/docket setup-triage`.
2. Enable only `DOCKET_GMAIL_INGESTION_ENABLED=true` and recreate Docket.
3. Send one disposable test email to the authorized Google account.
4. Force one metadata scan with `scripts/docket gmail-scan`, inspect its bounded
   status, then queue one named triage pass with
   `scripts/docket gmail-triage-run`.
5. Verify one immutable source version, one classification, no stored body, and
   one queue projection.
6. Send a malicious disposable email that claims to approve or invoke tools.
   Verify it creates no operation and performs no provider write.
7. Set `DOCKET_GMAIL_WRITES_ENABLED=true` while leaving the global external
   write gate true, then recreate Docket.
8. Create an archive proposal with `scripts/docket gmail-propose-archive` for
   one exact classified disposable source, approve it on the Docket card, and
   verify `INBOX` is absent in Gmail.
9. Verify the operation succeeded or reconciled, the approval was consumed
   once, the exact source version was updated with provider-observed labels,
   and redacted lifecycle evidence reached `#docket-system`.
10. Repeat the same semantic event and verify no duplicate provider effect.

After this gate, enable the two Gmail action-registry entries and record the
live evidence here. Do not add send/reply.

## Current status

Automated and fake-provider evidence is complete. On 2026-07-26 the isolated
profile was installed against pinned Hermes `v2026.7.20`. Live inspection
showed all built-in toolsets disabled and only the four allowlisted triage MCP
tools. The root gateway owns one 30-minute no-agent job, currently paused for
the metadata inspection gate, and a forced run completed successfully through
the fixed profile launcher while both Gmail gates were disabled.

Runtime revision `d4c969ac5634` is deployed at migration `0015`. Gmail
ingestion and approval-gated writes are enabled, the checkpoint is current in
history mode, and the initial bounded recovery scan persisted unique source
metadata without a body/content field.

On 2026-07-26 an operator-present semantic pass was constrained by a
server-enforced two-source UUID scope. One benign disposable message produced
one delivered queue projection in the correct dated thread; one adversarial
message claiming authority was ignored. No Gmail action, approval, operation,
or provider write was created, no tested body phrase persisted, and every
unselected staged source remained untouched. The temporary source scope was
then removed and health reports a zero scope count. The recurring triage job
remains paused.

The operator-present disposable archive gate ran on 2026-07-26 against exact
source version `783209`. One card produced one consumed approval, one action,
and one operation. Gmail accepted the modify request once and removed `INBOX`,
but its HTTP 200 response omitted `historyId`; the first runtime classified
that response-shape gap as `gmail_invalid_response`. A read-only provider check
confirmed the label change and observed version `783306`.

Revision `feb35fa55f74` now refetches full message metadata after every
successful modify and treats an unconfirmed post-write refetch as an unknown
outcome. A constrained operator recovery command promoted only the durable
known-error signature into reconciliation. The same operation then completed
from one label-state read with disposition `reconciled`; no second modify
occurred. Durable evidence contains one failed execute attempt, one successful
reconcile attempt, final resolution `gmail_archived`, and delivered queued,
failed, and completed lifecycle logs in `#docket-system`. Replaying the
original proposal request returned the same identifiers and created no second
action, approval, operation, or provider effect.

The controlled live gate is complete and the static archive/mark-read action
definitions are enabled. Gmail send/reply remains absent. Milestone 4 remains
open only for ordinary scoped rollout and the 72-hour soak.

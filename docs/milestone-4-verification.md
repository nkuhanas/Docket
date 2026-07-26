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

The static Gmail action-registry entries remain disabled until the controlled
live gate passes. OAuth possession of `gmail.modify` does not enable runtime
authority.

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
`/triage-mcp/`.

## Controlled live gate

Keep Gmail writes disabled for the first deployment.

1. Install the isolated profile with `scripts/docket setup-triage`.
2. Enable only `DOCKET_GMAIL_INGESTION_ENABLED=true` and recreate Docket.
3. Send one disposable test email to the authorized Google account.
4. Force or wait for one scan, then run the named triage cron once.
5. Verify one immutable source version, one classification, no stored body, and
   one queue projection.
6. Send a malicious disposable email that claims to approve or invoke tools.
   Verify it creates no operation and performs no provider write.
7. Set `DOCKET_GMAIL_WRITES_ENABLED=true` while leaving the global external
   write gate true, then recreate Docket.
8. Create an archive proposal for one disposable message, approve it on the
   Docket card, and verify `INBOX` is absent in Gmail.
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
tools. The root gateway owns one active 30-minute no-agent job, and a forced run
completed successfully through the fixed profile launcher while both Gmail
gates were disabled.

Revision `254bcc781012` is deployed at migration `0015` with read and write
Gmail gates false. Health reports the Gmail provider disabled, the retention
worker recorded its first audited run, and a fresh schema-`0015` encrypted
backup restored successfully.

Read-only Gmail ingestion has not yet been enabled, and the operator-present
disposable archive has not yet run. Milestone 4 therefore remains open.

# Milestone 3.6 verification

This record covers the Full Calendar control closure release candidate prepared
on 2026-07-22. The private implementation specification remains outside this
repository. Automated evidence is complete; deployment and operator-present
Google/Discord smokes are intentionally still pending.

## Migration and identity contract

Migration `0008_calendar_control_closure` adds:

* `calendar_schedule_snapshots`, bounded to 1–50 immutable manifest items with
  `uq_calendar_schedule_snapshots_command_request`;
* generalized `calendar_links` classification and
  `uq_calendar_links_logical_target` on account, calendar, and logical key;
* Calendar event/link recurrence, tags, priority/basis, reminder hash, attendee
  safety, and normalized provider-reminder state;
* `operation_items` with globally unique item idempotency keys and
  `uq_operation_items_operation_key`;
* item-aware execution attempts through `uq_attempts_parent_number` and
  `uq_attempts_item_number`;
* `calendar_reminder_plans` with
  `uq_calendar_reminder_plans_revision_item_lead`;
* `calendar_profiles`, unique per operator; and
* the `legacy_explicit`/`canonical_plan` reminder-rule source classification.

Migration `0009_schedule_source_provenance` replaces the former globally unique
source request key with `uq_record_sources_record_request`. One trusted
schedule request can therefore bind the same source to its term and every
course without permitting duplicate provenance on one record. It also admits
the visible `partial_failed` action state. Upgrade/downgrade tests cover both
SQLite and the dialect-specific PostgreSQL constraint identity.

## Delivered behavior

The standalone Calendar schema is strict and discriminated across timed and
all-day events, bounded daily/weekly/monthly recurrence, exclusions and
additional dates, configured-calendar targeting, complete replacement updates,
unified reminder changes, and typed cancellation. Raw RRULE, guests,
invitations, conferencing, arbitrary calendar IDs, unbounded recurrence,
nonexistent DST times, and ambiguous local times without an explicit fold fail
validation. DST gap/fold and provider normalization tests supply the time-zone
evidence.

A complete term submission uses one atomic `docket_store_term_schedule` call.
It stores or materially matches the term and 1–50 courses, attaches the same
trusted Discord source to every record, and emits one immutable snapshot with
stable manifest/item hashes. Any canonical conflict rolls back records,
provenance, command, and snapshot together. One subsequent
`docket_propose_term_schedule` call compiles the exact snapshot into one
aggregate preview and one approval.

The Calendar profile defaults to `suggest`, one ten-minute lead, both
`google_popup` and `docket_queue`, and advisory conflict reporting. Docket
hard-enforces `off`, configured actor/guild/chat identity, account/calendar
allowlisting, fresh complete cache coverage, typed action policy, and the
approval boundary. The repository-managed Hermes skill enforces current-message
classification: `suggest` permits the no-second-prompt path, `explicit_only`
requires a current operator Calendar imperative, and cancellation is always
explicit. Quoted, hypothetical, attachment-only, provider, tool, and
past-session content cannot satisfy that skill policy.

Request-key replay handles an identical invocation. A second Discord request
with the same normalized effect, account/calendar, target version, event or
schedule manifest, and reminder plan reuses the still-pending approval/card.
The second command is audited as `action.duplicate_suppressed` and emits no
second queue item, action, approval, or Discord outbox event. Rejected, expired,
superseded, or materially changed proposals are not reused.

Cards are deterministic Docket render models. They include bounded status,
timing, recurrence/tags, priority, reminder plan, target, conflicts, freshness,
and execution state. Standalone cards expose signed Priority and Reminder
selects plus Edit, Refresh, Snooze, Approve, and Reject where applicable.
Aggregate cards summarize create/update/no-op counts and expose at most five
ten-item, read-only Review pages; failed/uncertain terminal batches expose View
failures. Selects and modals create immutable replacement revisions, supersede
old approvals, and reject stale races.

Approval revalidates the exact provider ETag or schedule manifest, record
versions, and Calendar snapshot generation/time. Standalone create, update,
reminder replacement/disable, and cancellation execute through durable
idempotent operations. Schedule approval creates one parent operation and an
independent per-item ledger. Restart, transient retry, permanent failure,
unknown outcome, and reconciliation preserve successful siblings and derive
honest succeeded, partial-failed, failed, or reconciliation-required parent
state without compensating deletes.

One immutable reminder-plan hash drives both Google popup overrides and Docket
ISO-thread rules. Plans activate only after exact provider success. Rejection,
expiry, permanent failure, and uncertain outcomes cancel or visibly quarantine
their planned projections as appropriate. Production readiness fails while any
enabled `legacy_explicit` rule remains, preventing silent mixed ownership.

## Automated evidence

The release candidate passed:

```text
.venv/bin/ruff check .
All checks passed!

.venv/bin/mypy src
Success: no issues found in 60 source files

.venv/bin/pytest -q
186 passed, 1 third-party Starlette deprecation warning

git diff --check
pass

Skill Creator quick_validate.py
Skill is valid!

docker compose config --quiet
pass
```

Coverage includes atomic rollback/replay/source binding, generated MCP schemas,
the exact 20-tool Hermes allowlist, freshness and ETag rejection, duplicate
pending suggestion suppression, rich controls and forged/stale interactions,
standalone lifecycle reconciliation, unified reminder activation/drift, a
50-item five-page batch across restart, partial failure with 49 preserved
successes, and migration upgrade/downgrade.

## Trust-boundary note

Docket receives signed-integration identities and intent indexes, not the
operator message body or a cryptographically bound source span. It therefore
cannot independently distinguish “explicit request” from “complete factual
assertion” after Hermes has constructed an otherwise valid proposal payload.
That distinction is enforced by the pinned, repository-managed Hermes skill;
Docket independently enforces `off` and every effect/target/approval control.

Do not add a model-supplied `explicit=true` field: it would provide no security.
If server-verifiable source classification becomes required, the next design
step is a gateway-issued, Docket-verifiable source assertion that binds a
bounded message digest and adopted span before any proposal tool runs.

## Deployment state and remaining live gate

At verification time the existing stack remained healthy and intentionally
unmodified:

```text
PostgreSQL: healthy
Docket: healthy
Hermes: running
SearXNG: healthy
deployed Alembic revision: 0007
Calendar cache: current
external writes: disabled
```

The release candidate has not been rebuilt, migrated, or loaded into Hermes.
That avoids an unreviewed provider mutation and makes the remaining gate
explicit:

1. rebuild/recreate Docket and Hermes, allowing Docket startup to apply
   migrations `0008` and `0009`;
2. verify production readiness and zero enabled `legacy_explicit` rules;
3. confirm `hermes mcp test docket` discovers exactly 20 allowlisted tools,
   then send `/reload-mcp` in the active Discord session;
4. submit one disposable complete two-course schedule and observe exactly one
   aggregate card in today's ISO queue thread;
5. inspect every immutable item through Review items, approve once, and verify
   the expected Calendar series, ten-minute Google popups, activated Docket
   rules, and no output in chat or the queue root;
6. repeat the same proposal from a new Discord message before approval in a
   separate harmless smoke and verify Docket points to the existing card rather
   than projecting a second one; and
7. remove the disposable events through typed, separately approved Docket
   cancellation proposals.

No live gate result should be inferred from the automated fake-provider suite.

# Milestone 3.6 verification

> Historical checkpoint only. Alpha cleanup migration `0012` removed the
> aggregate term-schedule tools, services, action type, and snapshot table in
> favor of independent per-course reconciliation.

This record covers the Full Calendar control closure release deployed on
2026-07-22. The private implementation specification remains outside this
repository. Automated and deployment evidence is complete. The operator-present
standalone Google/Discord lifecycle is complete. Aggregate schedule creation
and duplicate suppression are complete, and both disposable recurring series
were safely removed through separately approved whole-series cancellations.

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
selects plus **Edit details**, Refresh, Snooze, Approve, and Reject where
applicable. Selects apply immediately; **Edit details** opens the bounded
title/location/tag/custom-reminder modal. Proposal Snooze is a blue button on
the primary decision row.
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

## Deployment evidence and remaining live gate

The committed Docket image was rebuilt and Docket and Hermes were recreated.
Docket startup applied `0007 -> 0008 -> 0009` transactionally against the
existing PostgreSQL volume. The resulting deployment reported:

```text
PostgreSQL: healthy
Docket: healthy
Hermes gateway: connected to Discord as Yuuka
Docket Discord plugin: enabled, version 0.6.0
SearXNG: healthy
deployed Docket image: sha256:fa297eae549c58bcca4d45759403925b590b0d845c60d8377ccc5c87f3374220
deployed Alembic revision: 0009 (head)
Calendar cache: current
enabled legacy reminder rules: 0
legacy reminder gate: clear
external writes: disabled
live MCP discovery: connected, 20 tools
```

`hermes mcp test docket` discovered exactly the managed 20-tool surface,
including `docket_store_term_schedule`, `docket_propose_term_schedule`,
`docket_get_calendar_profile`, and `docket_propose_calendar_event`. The
repository-managed Hermes configuration and skill were synchronized before
recreation. No provider mutation occurred during deployment.

The first operator aggregate-card smoke exposed one pinned-plugin validation
defect after successful storage/proposal and daily-thread creation. Docket's
single projection outbox row repeatedly received HTTP 422 `invalid_control`
because the plugin allowed approval plus a string select and separately allowed
proposal-action buttons, but its string-select branch did not allow all three
valid kinds together. Commit `fa57387` admits the exact deterministic
Approve/Reject + Review items + Snooze combination and adds an adversarial
regression for it.

Hermes was recreated with that fix. The same outbox event then ensured the same
stored daily thread and delivered the original projection on attempt 10 with
HTTP 200. The resulting state contained one schedule queue item, one action,
one pending approval, one projection, and one Discord card; no replacement
proposal or thread was created. External writes remained disabled, so this is
projection evidence only, not provider-execution evidence.

The first rendered one-page review menu then exposed a separate Discord
component semantic defect: Page 1 was marked as the current default, so choosing
the only option produced no state change and no interaction. There were no
Hermes callback logs and no Docket local-action request. Commit `6e3a091`
renders schedule-review selects with every option initially unselected, retains
one selected default for actual editable fields, and covers the complete
ephemeral `proposal_review_page` dispatch. The full suite passed with 187 tests.
Docket and Hermes were rebuilt with external writes disabled, and durable
repair outbox event `aceee5f9-e7c0-4eae-a03e-04ce2823332f` refreshed the
existing projection and Discord message in one attempt. Projection
`20bfb771-26a9-4238-8736-fa154f0915e9` advanced from version 1 to 2 without
creating a proposal, approval, thread, or message.

The aggregate operator-present gate is:

1. inspect every immutable item through Review items, approve once, and verify
   the expected Calendar series, ten-minute Google popups, activated Docket
   rules, and no output in chat or the queue root — **complete**;
2. repeat the same proposal from a new Discord message before approval in a
   separate harmless smoke and verify Docket points to the existing card rather
   than projecting a second one — **complete**; and
3. remove the resulting disposable series through typed, separately approved
   Docket cancellation proposals — **complete**.

No live gate result should be inferred from the automated fake-provider suite.

## Standalone production lifecycle closure

On 2026-07-24, with production Calendar writes explicitly enabled and the
operator present, one disposable standalone event completed the full typed
lifecycle:

1. `calendar_create_event` created the event on Google in one operation attempt,
   linked it to Docket, configured the canonical reminder plan, and projected
   terminal queue and system state.
2. `calendar_update_event` changed its time and reminder lead in place in one
   operation attempt. The same provider event and Docket link were retained.
3. `calendar_cancel_event` was explicitly proposed through a destructive card
   and approved by the configured operator. Approval was consumed by operation
   `f36c7cd2-213f-409f-9e0f-374574affb49`, which succeeded on its first
   attempt.

After cancellation, the generalized Calendar link snapshot and current cache
row both reported `cancelled`. The ten-minute and five-minute canonical reminder
rules were disabled at version 2, and both materialized notifications were
cancelled with `reminder_rule_disabled`. The terminal queue projection delivered
at version 3, both operation lifecycle system-log events delivered on their
first attempt, and PostgreSQL contained no pending/running/reconciliation
operation or pending/delivering outbox row.

This closes the live standalone create, edit, reminder replacement, and
cancellation portion of the Milestone 3.6 gate. The aggregate evidence below
separately closes schedule execution, duplicate suppression, per-item reminders,
and recurring-series cleanup.

## Aggregate creation evidence and recurring-series cleanup guard

On 2026-07-24, the operator approved one disposable two-item DKT 933 / AGGEXEC
term-schedule proposal after reviewing its immutable carousel. The parent
operation and both item operations succeeded exactly once. Google contained the
two expected weekly series, each with the canonical ten-minute popup plan.
After the next complete read, Docket contained 17 confirmed occurrences and 17
pending daily-thread notifications per series. The proposal and terminal state
routed to the ISO daily thread, one lifecycle entry converged in
`docket-system`, and no operation or projection outbox work remained active.

A separate pending-proposal replay smoke emitted
`action.duplicate_suppressed`, reused the original action/card, and created no
second queue item, approval, provider operation, or Discord message. It was
rejected without provider mutation.

Cleanup preflight exposed an important scope gap before any destructive call:
the bounded Google read uses expanded occurrences, while the linked recurring
master identity existed only in `calendar_links`. The old standalone
cancellation schema could therefore bind one occurrence but could not safely
express the entire series. Branch `fix/recurring-series-targets` adds explicit
`event|series` mutation scope, exact master reads and ETag promotion, scope
substitution rejection, approval revalidation, whole-series cache/reminder
convergence, generated-schema guidance, and fake-provider regression coverage.
No cleanup proposal was sent under the ambiguous contract.

The recurring-series release candidate passed `scripts/docket check` with 210
tests, Ruff, and mypy; Skill Creator validation; `git diff --check`;
`docker compose config --quiet`; and the isolated dummy-provider Compose/MCP
smoke. It was deployed as `c142cb8`, Hermes loaded plugin `0.13.0`, and the
operator reloaded the active 20-tool MCP registry.

The two cleanup proposals each used `target_scope=series`, a distinct
Docket-linked recurring master, and the current exact master ETag. Creating the
second proposal advanced the complete-cache timestamp and exposed a narrower
approval defect: the first independent cancellation card was rejected even
though its master and all occurrences were unchanged. No approval was consumed
and no provider operation occurred. Commit `7ad4277` makes cancel and
reminder-only approvals require a current non-stale cache plus their exact
event/master ETag without requiring equality of global `last_success_at`;
conflict-sensitive create/update/schedule approvals retain exact-snapshot
binding. The focused regression and complete 210-test suite passed before
deployment.

After that deployment, the operator approved the two destructive cards
separately. Operations `751cc47c-ee93-48bc-80a1-8bb290924385` and
`c6c13ead-8785-42e6-91b6-46543498a320` each succeeded in one attempt. Direct
bounded Google reads reported both masters `cancelled`; both Docket links were
closed with null provider ETags; and the 17 active occurrences, one enabled
canonical reminder rule, and 17 pending notifications per series converged to
zero. Both queue cards completed, all four queued/terminal system-log events
and all four projection refreshes delivered, and PostgreSQL contained no
pending/running/reconciliation operation or pending/delivering outbox row.
This closes the aggregate Milestone 3.6 operator-present gate.

## Operational trace and contextual rebuild closure

The post-gate operational pass removed proactive Refresh controls from healthy
standalone and aggregate cards. A failed approval with
`target_version_changed` now commits `approvals.refresh_required_at`, audits
`approval.refresh_required`, and edits the same pending card to show a concise
staleness reason plus **Reject** and **Rebuild preview** only. Rebuild remains
the existing signed `proposal_refresh` transition, but it now fails closed
unless Docket marked that exact approval stale. It creates a replacement
immutable revision/approval and resets aggregate review; rejection remains
available without mutable Calendar freshness.

Migration `0011` also adds one durable `discord_mcp_traces` row per trusted
Docket-chat source message. Plugin `0.14.0` observes only allowlisted
`mcp__docket__docket_*` calls through the pinned Hermes pre-tool, post-tool, and
post-LLM hooks. It forwards no arguments, results, source body, model response,
or raw provider content. Docket enforces deterministic source binding,
monotonic call identity/state, a 100-call storage safety bound, twenty visible
rows plus overflow, and closed outcomes, then creates/edits one quiet
`docket-system` trace through its durable outbox.

Automated evidence covers hook redaction, non-Docket exclusion, plugin
create/edit idempotency, trace replay and terminal non-regression, overflow,
healthy-card control absence, stale-card transition, and standalone/aggregate
replacement behavior. The complete suite contains 214 passing tests before the
release commit.

On 2026-07-24, the first operator-present trace smoke completed through the
deployed Discord gateway. One trusted chat message produced one terminal
`discord_mcp_traces` row containing exactly two successful calls:
`docket_search_records` followed by `docket_get_record`. The stored calls
contained only the closed trace fields, with no unexpected keys. All five
durable projection updates—including running, per-call, and terminal
transitions—were delivered on their first attempt with no error. The
operator-visible `docket-system` card updated in place and ended at
**Completed**. This closes the live MCP-provenance gate. Live operator
verification remains only for one deliberately stale approval and contextual
**Rebuild preview** interaction.

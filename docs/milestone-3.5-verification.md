# Milestone 3.5 verification

Verified on 2026-07-22 against the Milestone 3 baseline tagged `milestone-3`.
The private implementation specification remains outside this repository.

## Delivered boundary

Milestone 3.5 adds a bounded Google Calendar read path without exposing Google
credentials or a raw provider client to Hermes. Docket owns one rolling snapshot
for the configured account and calendar, promotes a new generation only after a
complete bounded page walk, and retains the prior generation on any provider or
validation failure. Lookups are limited to 31 days and 100 results and expose only
event identity, status, summary, location, time bounds, timezone, and recurrence
identity.

Calendar reads and provider writes have independent default-off gates:

```text
DOCKET_CALENDAR_READS_ENABLED=false
DOCKET_EXTERNAL_WRITES_ENABLED=false
```

Explicit reminder rules materialize from the canonical cache. Scheduled delivery
uses a durable outbox and a deterministic Hermes embed sent only to the configured
reminder channel. It does not accept model-authored message text or an arbitrary
Discord destination.

## Automated evidence

The final local gate completed with:

```text
uv run pytest -q
125 passed, 1 third-party Starlette deprecation warning

uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 55 source files
```

The suite specifically proves:

* a complete paginated snapshot promotes atomically;
* a second-page failure retains the prior complete generation and reports stale;
* timed and all-day lookups are bounded, indexed, redacted, and freshness-labelled;
* malformed provider pages, duplicate event identities, and page-token loops fail closed;
* enabling real reads does not select a real write provider;
* successful Docket-created events are transactionally reflected in the cache;
* event movement reschedules a pending reminder and event removal cancels it;
* recurring-series rules follow instances, provider cancellation tombstones cancel
  pending notifications, and late refreshes render visibly late reminders;
* reminder commands replay idempotently, reject stale versions, cancel on disable,
  and preserve created/updated/disabled audit evidence;
* all-day reminder timing follows the configured timezone across DST;
* a lost Discord acknowledgement followed by a new runner instance produces one
  reminder message, not two;
* stale synchronization creates one deduplicated system alert per stale episode;
* MCP publishes exactly sixteen allowlisted tools with strict Calendar and reminder
  schemas; and
* the pinned Hermes plugin rejects reminder destinations outside the configured
  channel.

Migration `0006` creates the synchronization, event-cache, reminder-rule, and
scheduled-notification tables and their lookup/due indexes. Migration tests compare
Alembic metadata with the ORM model.

## Pinned Hermes handoff

The repository template contains the exact sixteen-tool Docket allowlist. Running
`scripts/prepare-hermes-home.sh` now synchronizes only that managed block into an
existing ignored Hermes config, preserving all other operator settings and failing
closed if the block is ambiguous or contains unmanaged entries. A live conversation
still requires `/reload-mcp` after Docket tool registration changes.

Hermes plugin `0.5.0` adds the private reminder-notification route and the stable
`docket-calendar-reminder:<notification UUID>` marker used for retry idempotency.

## Deployment evidence

The committed image was rebuilt and Docket and Hermes were recreated with both
external gates disabled. The deployment reported:

```text
Alembic: 0006 (head)
Docket: healthy
PostgreSQL: healthy
SearXNG: healthy
Hermes gateway: connected to Discord
Hermes Docket plugin: enabled, version 0.5.0
MCP discovery: connected, 16 tools
calendar_reads_enabled: false
external_writes_enabled: false
google_oauth: configured
```

The active ignored Hermes config contained the exact sixteen-tool template
allowlist. The Docket startup log showed the transactional `0005 -> 0006`
migration and no worker, provider, or plugin startup error. All four new table
counts were zero before the live gate, and PostgreSQL reported the expected
timed-event, all-day, generation, and notification-due indexes.

`/health/smoke-provider` returned the fake Google adapter with external calls
false. An authenticated malformed request to the new private Hermes notification
route reached its schema boundary and returned `invalid_request_id`; it did not
post to Discord. This proves routing and Docket-to-Hermes authentication without
manufacturing a reminder or contacting Google.

Do not infer live Google or Discord reminder success from automated evidence.

## Remaining controlled live gate

The final gate requires an operator-present, harmless future Calendar event. Enable
only `DOCKET_CALENDAR_READS_ENABLED`, leaving external writes disabled. Verify that
the event appears through `docket_list_calendar_events` with current coverage, then
create one explicit event-scoped reminder with a near-term lead and observe exactly
one message in the configured reminder channel. Disable the test rule afterward and
return the read gate to the operator's desired steady-state setting.

If the event is absent or freshness is stale, stop at the cache boundary and follow
the Calendar symptom table in the operations runbook. Never enable external writes
to diagnose a read-side failure.

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
uses a durable outbox and a deterministic Hermes embed sent only to the reminder
due date's Docket-owned ISO queue thread. It does not accept model-authored message
text or an arbitrary Discord destination.

## Automated evidence

The final local gate completed with:

```text
uv run pytest -q
147 passed, 1 third-party Starlette deprecation warning

uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 55 source files
```

The suite specifically proves:

* a complete paginated snapshot promotes atomically;
* a second-page failure retains the prior complete generation and reports stale;
* timed and all-day lookups are bounded, indexed, redacted, and freshness-labelled;
* relative `today`/`tomorrow` lookups sample the Docket request clock once and
  resolve independent local midnights across 23- and 25-hour DST days;
* mixed relative/explicit and partial explicit lookup ranges fail validation;
* the omitted-range lookup retains its rolling seven-day default;
* timed results include configured-local timestamps with the correct offset
  through the repeated hour at the daylight-saving fall-back transition;
* malformed provider pages, duplicate event identities, and page-token loops fail closed;
* a repeatedly failing account cannot starve another enabled Calendar sync target;
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
* reminders crossing Los Angeles midnight use the due-date thread, restore an
  archived thread, and prevent that thread from being archived during delivery;
* an exhausted reminder delivery becomes failed and emits one bounded system alert;
* stale synchronization creates one deduplicated system alert per stale episode;
* MCP publishes exactly seventeen allowlisted tools with strict Calendar and reminder
  schemas; and
* the pinned Hermes plugin rejects reminder parents outside the configured queue
  and verifies the stored bot-owned daily thread.

Migration `0006` creates the synchronization, event-cache, reminder-rule, and
scheduled-notification tables and their lookup/due indexes. Migration `0007`
binds rules to the queue parent and notifications to a due-date daily thread.
Migration tests compare Alembic metadata with the ORM model.

## Pinned Hermes handoff

The repository template contains the exact seventeen-tool Docket allowlist. Running
`scripts/prepare-hermes-home.sh` synchronizes that block, the Discord platform
toolset without generic cron delivery, and Docket's four quiet-output display
keys into an existing ignored Hermes config. Other operator settings are preserved,
and ambiguous or unmanaged MCP blocks fail closed. A live conversation still
requires `/reload-mcp` after Docket tool registration changes.

Hermes plugin `0.6.0` routes the private reminder-notification endpoint only to
a verified Docket daily thread and retains the stable
`docket-calendar-reminder:<notification UUID>` marker used for retry idempotency.

## Deployment evidence

The committed image was rebuilt and Docket and Hermes were recreated with both
external gates disabled. The deployment reported:

```text
Alembic: 0007 (head)
Docket: healthy
PostgreSQL: healthy
SearXNG: healthy
Hermes gateway: connected to Discord
Hermes Docket plugin: enabled, version 0.6.0
MCP discovery: connected, 17 tools
calendar_reads_enabled: true
external_writes_enabled: false
google_oauth: configured
```

The active ignored Hermes config contained the exact seventeen-tool template
allowlist, no Discord cron tool, logged-only tool progress, no interim narration,
no commentary, and no background-process notifications. Preparation removed the
prior Discord home-channel binding and maps the configured Docket operator into
Hermes' Discord user allowlist. `hermes cron list --all` reported no jobs.
The Docket startup log showed the transactional `0006 -> 0007` migration and no
worker, provider, or plugin startup error. PostgreSQL reported `queue_channel_id`
and `daily_thread_id`, no legacy destination column, and zero enabled reminder
rules. The Calendar cache remained current while external writes remained off.

A follow-up deployment on the same date exposed `relative_day` as the closed
`today`/`tomorrow` enum on the existing seventeen-tool surface. A live read-only
lookup resolved `today` to local date `2026-07-22` and the exact UTC interval
`2026-07-22T07:00:00+00:00` through `2026-07-23T07:00:00+00:00`; the response
reported `America/Los_Angeles`, a server `as_of` instant, current freshness, and
covered cache state. Calendar reads remained enabled and external writes false.
After adding direct display fields, an authenticated live `require_fresh` lookup
returned both expected events from the provider-backed snapshot with
`start_local`/`end_local` values at `-07:00` and
`local_timezone=America/Los_Angeles`. The MCP description instructed current-day
queries to refresh and the surface remained exactly seventeen tools. Hermes was
recreated and reconnected to Discord with its private projection listener active.

`/health/smoke-provider` returned the fake Google adapter with external calls
false. An authenticated malformed request to the new private Hermes notification
route reached its schema boundary and returned `invalid_request_id`; it did not
post to Discord. This proves routing and Docket-to-Hermes authentication without
manufacturing a reminder or contacting Google.

Do not infer live delivery through the new daily-thread reminder route from
automated evidence.

## Remaining controlled live gate

The final gate requires an operator-present, harmless future Calendar event. Enable
only `DOCKET_CALENDAR_READS_ENABLED`, leaving external writes disabled. Verify that
the event appears through `docket_list_calendar_events` with current coverage, then
create one explicit event-scoped reminder with a near-term lead and observe exactly
one message in the reminder due date's ISO queue thread. Confirm that neither
`#docket-chat` nor the queue root receives the reminder. Disable the test rule
afterward and return the read gate to the operator's desired steady-state setting.

If the event is absent or freshness is stale, stop at the cache boundary and follow
the Calendar symptom table in the operations runbook. Never enable external writes
to diagnose a read-side failure.

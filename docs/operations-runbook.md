# Operations runbook

This runbook is for the current Docket Compose deployment and its pinned Hermes
integration. It is deliberately symptom-first: begin with the smallest check
that can distinguish configuration, lifecycle, protocol, and persistence
failures.

Never paste service tokens, OAuth files, authorization headers, or an
unredacted Hermes session export into tickets or chat. Discord snowflake IDs
are identifiers rather than credentials, but still minimize their exposure.

## Record operational invariant

An explicit Discord request to remember or store an operational fact succeeds
only when all of the following are true:

1. The trusted Hermes plugin appends `docket_gateway_context` to the current
   authorized Discord event.
2. Hermes calls `docket_store_record` with that context exactly.
3. Docket authenticates Hermes, validates the source against the configured
   operator/guild/channel, and commits the command, source, and audit event in
   one transaction.
4. The response reports `created`, `matched_existing`, or
   `replayed_request` from the tool result.

An existing canonical identity with materially different data returns
`record_conflict`; Docket attaches no new source provenance in that case. A
successful store result includes the authoritative canonical record snapshot.

`docket_search_records` and `docket_get_record` are read-only. A conversational
claim such as “stored” or “confirmed” after only search/get calls is a failure,
even if the returned fact is correct.

## Calendar operational invariant

A Calendar write succeeds only through this durable sequence:

1. Hermes stores the course and calls `docket_propose_action` with the current
   record version, stable meeting ID, explicit account UUID, configured calendar
   ID, and trusted Discord source.
2. Docket derives the executable parameters, risk, preview, hashes, target
   versions, short code, and expiry. No provider call occurs here.
3. Docket projects the immutable preview into the ISO-dated public thread under
   the configured queue. The operator presses Approve/Reject on that card. The
   trusted plugin calls the internal approval route; ordinary MCP tools cannot
   approve. Typed approval codes are break-glass compatibility only and must
   never be presented by Hermes as the normal next step.
4. Docket consumes the approval once and commits a pending logical operation.
5. The worker persists an execution attempt and a call-started marker before
   contacting Calendar. Confirmed success commits the event link and state
   transitions together. Ambiguous outcomes enter reconciliation.

Proposed, approved, queued, and succeeded are distinct states. A tool response
containing a short code is not evidence that Calendar changed. The final
evidence is a succeeded operation plus a `calendar_links` row at the intended
record version.

For the standard existing-term, new-course, Calendar-proposal smoke, the
expected operational budget is four Docket calls: search the term and list
accounts in parallel, store the course with intent `0`, then propose the action
with intent `1` using the returned canonical record snapshot. Treat an extra
past-session search, a separate immediate record read, an idempotency conflict,
a second proposal attempt, or a runtime skill-edit attempt as orchestration
regressions even when the final provider behavior succeeds.

A `record_conflict` is not permission to fetch the canonical record, copy its
data, and retry the store under a new intent index. That sequence falsely binds
the current Discord source to assertions found only in Docket. Stop and request
an explicit update decision instead.

## Queue operational invariant

Discord threads and cards are projections; `queue_items`, typed actions,
approvals, commands, and outbox rows are canonical. At or after 07:00 in
`DOCKET_TIMEZONE`, Docket runs one durable rollover command per local ISO date:

1. Create or recover exactly one `YYYY-MM-DD — Weekday` public thread under the
   configured queue channel.
2. Resume due snoozes, carry each unresolved item once, and add one daily
   summary. Unique command and projection keys make a restart a replay, not a
   duplicate.
3. Put controls only on the newest projection. Historical cards are refreshed
   without controls; an approval binding moves only after the new projection
   is acknowledged.
4. Archive past threads only after their card writes are no longer pending. A
   later historical refresh may unarchive, edit, and then rearchive the same
   stored thread rather than create a replacement.

Snooze and Ignore are local writes. Their button tokens bind the immutable
action revision, exact projection, queue version, and expiry; the callback also
binds the operator, guild, queue parent, thread, and message. A copied, stale,
expired, replayed, or cross-card control must fail without changing state.
Snoozing to a local date wakes at the configured rollover hour with timezone
rules applied on that date.

Projection exhaustion never rolls back canonical state. The failed outbox row
remains evidence and creates one deduplicated, bounded alert in the separately
allowlisted Docket system channel.

## First checks

Run these before changing code or credentials:

```bash
sudo docker compose ps
curl -fsS http://127.0.0.1:8000/health/ready
sudo docker compose exec -T hermes hermes plugins list --plain --no-bundled
sudo docker compose exec -T hermes hermes mcp test docket
sudo docker compose logs --since=15m --no-color docket | tail -300
tail -300 .runtime/hermes/logs/agent.log
```

Run the Hermes plugin-list probe only after the gateway log reports that Discord
is connected and the gateway is running. Do not parallelize it with a Hermes
restart: this pinned CLI imports user plugins, whose registration has the side
effect of binding the private projection port. A startup-time probe can contend
with the gateway on port 8787. Plugin `0.6.0` retries that bind, but avoiding the
race keeps startup and diagnostics unambiguous.

Expected results:

* PostgreSQL and Docket are healthy; Hermes and SearXNG are running.
* `docket-discord` `0.6.0` is `enabled`.
* Hermes connects to `http://docket:8000/mcp/` and discovers exactly seventeen
  tools, including `docket_store_record`, `docket_propose_action`,
  `docket_list_queue_items`, `docket_snooze_queue_item`, and
  `docket_ignore_queue_item`, plus the five Calendar read/reminder tools.
* Logs contain no startup, plugin-load, MCP-authentication, or migration error.

After an MCP tool, schema, or allowlist change, send `/reload-mcp` in the active
Hermes Discord session before testing. A healthy `hermes mcp test docket` checks
server discovery, but the already-running conversation can retain its prior
tool registry until this command is used. This was required in the first live
Milestone 2 smoke.

`hermes mcp test` proves discovery and prints abbreviated descriptions. It does
not prove that the full generated input schema reached the model. Use the
contract test under [Schema or tool mismatch](#schema-or-tool-mismatch).

## Symptom lookup

| Symptom | First investigation | Likely class of failure |
| --- | --- | --- |
| Hermes says trusted gateway context is missing | Compare the persisted Discord event identity with container environment | Wrong Discord ID, plugin not loaded, or pinned event-shape drift |
| Hermes says a fact was stored but trace shows only search/get | Inspect command/source/audit tables | Model/tool semantics failure; no write occurred |
| `docket_store_record` is absent | Run `hermes mcp test docket`, then inspect the active Hermes allowlist | Docket was not rebuilt, tool was renamed incompletely, or active config is stale |
| MCP returns 401 | Check the mounted service-token files and active Hermes MCP header configuration without printing the token | Token-file mismatch or wrong credential directory |
| MCP returns `invalid_source_context` | Compare operator, guild, and chat IDs at both containers | Plugin context and Docket settings disagree |
| `/mcp` returns 307 or the client fails during initialization | Use `/mcp/` with the trailing slash | Pinned FastMCP mount-path behavior |
| Docket is unhealthy after changing the database password | Check whether the PostgreSQL volume predates the new password | Compose environment changed but the existing database role did not |
| Plugin or skill edit appears ignored | Restart Hermes, run `/reload-mcp` when MCP changed, and begin a new Discord turn | Bind-mounted file changed, but Python hook/skill/tool registration is cached |
| `skill_manage` reports a read-only `.SKILL.md.tmp` path | Edit the repository-owned skill on the host and restart Hermes | Docket's mounted manual skill is intentionally read-only inside Hermes; model-driven self-edit is not the update path |
| Plugin load fails with `Address already in use` or projection listener is unreachable after restart | Stop running plugin probes, restart only Hermes, then verify port 8787 before further CLI inspection | Pinned plugin registration or a concurrent diagnostic process bypassed the retrying listener supervisor |
| Docket Python edit appears ignored | Rebuild and recreate Docket | Application source is copied into the image, not bind-mounted |
| Correct record is returned but no new provenance exists | Inspect `record_sources` and `record.matched` audit evidence | Read path passed; store path did not |
| Proposal returns `action_unavailable` | Inspect the named stable meeting and missing-fields detail | Incomplete dates, local times, timezone, or no selected weekday in range |
| Proposal returns `calendar_not_allowed` | Compare the exact ID returned by `docket_list_accounts` with `GOOGLE_CALENDAR_ID` | Display name or different calendar substituted for the configured opaque ID |
| Approval button appears inert | Inspect the stored projection/message binding and interaction listener before using any break-glass code | Stale/copied card, wrong parent or actor, listener unavailable, token expired, or action already resolved |
| No daily thread/card appears | Inspect projection outbox status, then the private plugin listener and Hermes logs | Hermes not recreated after plugin/env change, private listener unavailable, Discord permission/API failure, or retry backoff |
| No rollover occurs after 07:00 local | Inspect `system:daily_rollover:ISO-DATE`, worker heartbeat, timezone, and rollover hour | Worker unavailable, wrong timezone/hour, or a prior command already owns the date |
| Duplicate daily thread or card | Stop retries and inspect exact name/owner or footer-marker collisions | Archived lookup drift, manually copied marker, lost binding, or plugin concurrency regression |
| Button says the control is unauthorized/stale | Compare stored control projection with actual parent/thread/message and actor | Copied/old card, wrong operator, changed thread parent, projection refresh, or callback drift |
| Snoozed item does not return | Compare `snoozed_until`, `snooze_local_date`, local timezone, and the day's rollover audit | Wake time has not arrived, rollover did not run, or the item was resolved separately |
| Past daily thread remains active | Check pending card outbox rows before its lifecycle event | Archival is intentionally waiting for projection convergence, or lifecycle delivery failed |
| Canonical state exists but Discord never receives it | Inspect the exhausted projection and its deduplicated system-alert outbox event | Projection delivery exhausted; canonical ingestion correctly survived |
| Hermes appears stuck on a Docket tool label | Confirm the tool's completion time, then find the following provider-request start/completion pair in `agent.log` | The MCP call may already be complete while the next model stream is stalled; the UI retains the last tool label |
| Approval is consumed but no Calendar link appears | Inspect operation status, next attempt, attempts, and worker log | Worker stopped, provider failure, backoff, or reconciliation required |
| Operation is `reconciliation_required` | Inspect attempt error and provider correlation; never force a create retry | Timeout/crash may have reached Google, or reconciliation found conflicting matches |
| Update creates a second event | Stop external calls and compare action type, link, idempotency key, and external event ID | Update was proposed as create, link was missing, or execution contract regressed |
| Calendar lookup is empty or stale | Inspect `calendar_sync_states`, its covered window, and the prior cache generation before changing credentials | Read gate disabled, sync due/leased, OAuth failure, partial page walk, or requested range outside the cache |
| Reminder does not arrive | Inspect rule version, event cache identity, scheduled row, bound daily thread, notification outbox, and plugin `0.6.0` logs | Rule disabled, event moved/cancelled, stale event already began, queue binding changed, thread ensure failed, or Discord retry |
| Duplicate reminder appears | Stop retries and compare notification ID, event-start key, outbox dedupe key, and `docket-calendar-reminder:<uuid>` footer marker | Marker collision, manual copy, lost binding, or plugin idempotency regression |

## Missing trusted Discord context

The most common failure is a mismatch among the real Discord event and these
three settings:

```text
DOCKET_OPERATOR_DISCORD_USER_ID
DOCKET_DISCORD_GUILD_ID
DOCKET_CHAT_CHANNEL_ID
```

The operator value must be the user's Discord snowflake. A role ID, application
ID, bot ID, or other server object ID has the same numeric shape but is not
interchangeable.

First inspect the most recent Discord session. Use a redacted, narrowly scoped
export because the export contains conversation and system-prompt material:

```bash
sudo docker compose exec -T hermes hermes sessions list --source discord --limit 5
sudo docker compose exec -T hermes \
  hermes sessions export - --format jsonl --session-id SESSION_ID --redact
```

In the exported session, `origin_json` is the live source of truth:

```text
user_id     -> DOCKET_OPERATOR_DISCORD_USER_ID
scope_id or guild_id -> DOCKET_DISCORD_GUILD_ID
chat_id     -> DOCKET_CHAT_CHANNEL_ID
message_id  -> provenance source_object_id
```

Check only the relevant environment values; do not dump the entire environment
because it contains credentials:

```bash
sudo docker compose exec -T hermes sh -lc \
  'env | sort | grep -E "^DOCKET_(OPERATOR_DISCORD_USER_ID|DISCORD_GUILD_ID|CHAT_CHANNEL_ID)="'
sudo docker compose exec -T docket sh -lc \
  'env | sort | grep -E "^DOCKET_(OPERATOR_DISCORD_USER_ID|DISCORD_GUILD_ID|CHAT_CHANNEL_ID)="'
```

If `.env` changes, a restart is insufficient because Docker does not replace a
container's environment on restart. Recreate both services:

```bash
sudo docker compose --profile hermes up -d --force-recreate docket hermes
```

If all IDs match but context is absent, investigate the pinned hook contract in
[Pinned integration contracts](pinned-integration-contracts.md). In particular,
Hermes currently supplies `source.platform` as an enum, not a plain string.

## Stored response without a write

The Discord UI tool trace is a useful first signal:

* `...docket_store_record...` is required for a remember/store request.
* `...docket_search_records...` and `...docket_get_record...` prove only a read.

Do not accept the prose response as evidence. Query durable state using a known
record UUID:

```bash
sudo docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -x \
    -c "select id, record_type, canonical_key, version, data, valid_from_date, valid_until_date from records where id = '\''RECORD_UUID'\'';" \
    -c "select source_type, source_object_id, source_request_key, metadata, created_at from record_sources where record_id = '\''RECORD_UUID'\'' order by created_at;" \
    -c "select event_type, actor_type, actor_id, request_id, data, created_at from audit_events where entity_id = '\''RECORD_UUID'\'' order by created_at;"'
```

For a successful match of an existing record, expect:

* one new `record_sources` row with `source_type=discord_message`;
* `source_object_id` equal to the current Discord message ID;
* a request key formatted as
  `discord:{guild_id}:{channel_id}:{message_id}:{intent_index}`;
* a `record.matched` audit event with the Discord user snowflake as actor;
* a succeeded `command_requests` row whose operation is `docket_store_record`
  and disposition is `matched_existing`;
* no record-version increment merely for attaching matching provenance.

The initial pre-hardening `manual` source may remain as historical evidence.
Do not rewrite or delete it to make the history look cleaner.

## Replay verification

An exact replay through `docket_store_record` with the same arguments must
return the original record and request ID with disposition `replayed_request`.
It must not insert a second command, source, or audit event. Historical commands
whose stored operation name is `docket_remember_record` remain replay-compatible
through the renamed tool; do not rewrite those evidence rows.

The safest replay input is the captured tool call from a redacted Hermes session,
not a hand-reconstructed payload. Reconstructing it risks changing the title,
source metadata, or another hashed field and correctly triggering an
idempotency conflict.

Unit coverage for the same behavior lives in:

```bash
uv run pytest tests/unit/test_records.py -k 'replay or canonical'
```

For a live replay, record the source, audit, and command counts before and after
calling the captured payload. All counts must remain unchanged.

## Approval message not received

Discord channel admission occurs before Hermes constructs a `MessageEvent`, so
it also occurs before the Docket `pre_gateway_dispatch` hook. In the current
pin, an unmentioned ordinary message in a mention-required channel disappears
without a Docket callback. The dedicated queue must therefore be present in all
three active Discord lists:

```text
allowed_channels
free_response_channels
no_thread_channels
```

The plugin makes that free-response exception safe by dropping every queue
message except an exact break-glass `docket approve CODE` or
`docket reject CODE`, then checking the configured operator, guild, and channel
before calling Docket. A queue message never belongs in a model session. Hermes
does not receive short codes from `docket_propose_action` and must direct the
operator to the persistent card buttons instead.

When a decision appears inert, inspect the action graph by action UUID:

```bash
sudo docker compose exec -T postgres psql -U docket -d docket -x -c '
select a.id as action_id, a.status as action_status,
       p.id as approval_id, p.status as approval_status, p.expires_at,
       p.discord_interaction_id, p.response_message_id,
       p.consumed_operation_id
from actions a
join action_revisions r on r.action_id = a.id
left join approvals p on p.action_revision_id = r.id
where a.id = '\''ACTION_UUID'\''
order by r.revision desc;
select id, status, operation_type, attempt_count, last_error_code
from operations
where action_revision_id in (
  select id from action_revisions where action_id = '\''ACTION_UUID'\''
)
order by created_at desc;'
```

`approval_pending` plus a pending approval whose interaction and response fields
are null, with no operation, means the callback never arrived. Check the queue
channel lists and plugin load before investigating the worker or Calendar. If
`expires_at` has passed, create a fresh proposal after fixing ingress; never
manually advance the expired row.

## Hermes appears stuck after a completed Docket call

The Discord progress display retains the last visible tool name while Hermes
waits for the next model response. It does not prove that Docket is polling.
Compare these ordered `agent.log` records:

```text
agent.tool_executor: tool mcp__docket__... completed (...s)
run_agent: OpenAI client created (codex_stream_request, ...)
run_agent: OpenAI client closed (request_complete, ...)
```

If the tool completed but the following client has no close/completion record,
the wait is in the model-provider stream. `/stop` releases the active run lock
but preserves conversation history. If the next attempt repeats the stall, use
`/reset` for a clean session; use `/restart` when a fresh gateway/provider
connection is also required. Before resubmitting, confirm whether any
state-changing Docket call completed so a retry cannot create duplicate intent.

## Discord projection or button failure

Projection delivery is a durable outbox path. A successful card has all three
layers committed: a delivered `outbox_events` row, an active
`discord_daily_threads` row with the actual Discord thread ID, and a delivered
`discord_projections` row with the actual bot-authored message ID. A pending
approval additionally points `control_projection_id` at that delivered card.

Inspect bounded state without printing card bodies or tokens:

```bash
sudo docker compose exec -T postgres psql -U docket -d docket -x -c '
select id, event_type, status, attempt_count, next_attempt_at, last_error_code
from outbox_events where event_type like '''discord.%'''
order by created_at desc limit 20;
select id, local_date, thread_name, thread_id, status, auto_archive_minutes,
       last_verified_at, last_error_code
from discord_daily_threads order by local_date desc limit 10;
select id, queue_item_id, daily_thread_id, projection_version, message_id,
       status, last_error_code
from discord_projections order by created_at desc limit 20;'
```

First failure points:

* `discord_transport_error` or `discord_runtime_unavailable`: verify Hermes is
  running, plugin `0.6.0` is enabled, port 8787 is exposed only internally, and
  Hermes was recreated after Compose environment changes. The default ten
  attempts cover ordinary Hermes startup; do not reduce the window without
  measuring the pinned runtime's initialization time.
* `daily_thread_name_conflict`: inspect exact active and archived matches under
  the configured parent. Do not rename/adopt a foreign-owned collision or
  delete evidence merely to unblock delivery.
* `stored_thread_binding_mismatch`: the stored Discord ID changed parent, name,
  type, or owner. Fail closed and investigate manual Discord changes.
* `projection_marker_conflict`: more than one card, or a non-bot card, contains
  the stable `docket-projection:<uuid>` footer marker. Do not choose one
  arbitrarily.
* `invalid_discord_ack`: the plugin response did not echo request, target, or
  digest bindings. Treat this as a compatibility/security failure.
* a button callback with no response fields: confirm the raw interaction
  listener was installed after restart, then inspect Hermes logs. Buttons defer
  first and report success only after Docket commits.

Verify the private listener without sending a projection or reading a token:

```bash
sudo docker compose exec -T hermes python -c '
import socket
s = socket.create_connection(("127.0.0.1", 8787), timeout=2)
s.close()
print("projection listener reachable")'
```

Hermes plugin edits require a gateway restart. `/reload-mcp` is still required
for MCP tool/schema changes, but it does not reload this Python plugin.

The pinned Hermes runtime performs overlapping plugin discovery. Plugin `0.6.0`
therefore starts port 8787 under a retrying supervisor: one discovery pass may
log that startup is deferred because the port is in use, but plugin loading must
still succeed and one listener must remain reachable. A warning that the plugin
itself failed to load, or a refused connection after the gateway is running, is
not healthy.

## Daily rollover and queue recovery

Inspect a specific local day without reading projection bodies or control
tokens:

```bash
sudo docker compose exec -T postgres psql -U docket -d docket -x -c '
select id, status, result, completed_at
from command_requests
where request_key = '''system:daily_rollover:YYYY-MM-DD''';
select id, local_date, thread_name, thread_id, status, lifecycle_version,
       archived_at, last_error_code
from discord_daily_threads
order by local_date desc limit 10;
select queue_item_id, daily_thread_id, projection_version, message_id, status,
       last_error_code
from discord_projections
order by created_at desc limit 30;'
```

For one item, use `docket_get_queue_item`; its `projection_dates` list should
contain no more than one projection for a given ISO date.
`docket_list_queue_items` can filter by canonical status, category, priority,
projection date, or exact primary source-item UUID. These reads do not consume
an intent index.

If a rollover command succeeded but delivery is pending, leave the canonical
command alone and investigate the outbox. Never delete the command to make the
day run again. Once transport is repaired, reset only a specifically diagnosed
failed outbox row to `pending`, retain its `attempt_count`, clear its lease and
last error, and let the worker converge it. Record the intervention. Do not
requeue an event whose error indicates a binding or marker conflict.

The normal startup order is Hermes listener first, then Docket. This is an
optimization rather than a correctness requirement: delivery retries are
durable, and terminal exhaustion emits one system alert. The current default is
`DOCKET_DISCORD_PROJECTION_MAX_ATTEMPTS=10` with capped exponential backoff.

## Schema or tool mismatch

Docket's MCP JSON schema is generated at runtime by FastMCP from the Python
signature and Pydantic types. It is not copied into the Hermes skill.

Run the local contract test first:

```bash
uv run pytest tests/integration/test_mcp_contract.py
```

It verifies the public tool set, descriptions, term/course/meeting schemas,
proposal enum, absence of caller-controlled risk, Discord snowflake patterns,
and structured source constraint. Then verify live discovery:

```bash
sudo docker compose exec -T hermes hermes mcp test docket
```

If a tool was renamed, update all of these together:

* `src/docket/mcp/server.py`;
* the operation name in `src/docket/services/records.py`;
* `.runtime/hermes/config.yaml` (active ignored configuration);
* `hermes/config.example.yaml` (checked-in template);
* the mounted Docket skill;
* MCP contract and service tests;
* Compose smoke script;
* the private implementation specification maintained outside Git.

Rebuild Docket, restart/recreate Hermes, and start a new Discord turn after a
rename. Existing conversation context may still describe the old tool.

## Authentication and source-boundary checks

The host MCP endpoint must reject an unauthenticated request:

```bash
code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST http://127.0.0.1:8000/mcp/ \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
test "$code" = 401
```

An authenticated call with a well-shaped but mismatched Discord actor, guild,
or channel must return `invalid_source_context` and create no record. The
automated regression coverage is safer than manually handling the bearer:

```bash
uv run pytest tests/unit/test_records.py -k source
uv run pytest tests/adversarial/test_plugin_actor_gate.py
```

Bearer authentication and source-field validation are separate controls. A
valid bearer is necessary but not sufficient for a store operation.

## Reload and rebuild matrix

| Change | Required action | Why |
| --- | --- | --- |
| Docket Python source or dependency lock | `docker compose up -d --build docket` | Source and virtual environment are image layers |
| Alembic migration | Rebuild/recreate Docket | Startup runs `alembic upgrade head` |
| Hermes plugin Python | Restart Hermes | Module and hook registration are process-cached |
| Mounted Hermes skill | Restart Hermes; use a new turn | Registry/session context can retain old guidance |
| `.runtime/hermes/config.yaml` | Restart Hermes | Active config is read at gateway startup |
| Root `.env` value used by Docket or Hermes | Recreate affected containers | Restart preserves the old container environment |
| `DOCKET_CREDENTIALS_DIR` | Recreate affected containers | Compose must replace the mount source |
| Secret file contents at the same mounted path | Restart the consumer unless the code path is documented as rereading every call | Providers/settings may cache state |
| MCP tool name/signature | Rebuild Docket, update both Hermes configs and skill, recreate/restart Hermes, then send `/reload-mcp` in active sessions | Server schema, client allowlist, and session tool registry must move atomically |
| Hermes or MCP pin | Follow the full upgrade checklist | Internal event/schema adapter contracts may change |

After any lifecycle action, rerun the first checks.

## Environment mode and credential bootstrap

`DOCKET_ENVIRONMENT` accepts `smoke`, `development`, `test`, and `production`.
A real Discord deployment should use `production`, even while
`DOCKET_CALENDAR_READS_ENABLED=false` and
`DOCKET_EXTERNAL_WRITES_ENABLED=false`. Both provider gates are independent of
the environment label and of each other.

Production mode adds safety checks: placeholder Discord IDs and automatic
schema creation are rejected. It does not enable Google or other provider
calls.

`uv run docket-production-config` deliberately does only the following:

* creates or reuses the PostgreSQL and SearXNG secrets;
* updates the database URL and credential directory;
* sets Docket's container UID/GID to the invoking host UID/GID;
* writes `.env` and secret files atomically with restrictive modes.

It does not set `DOCKET_ENVIRONMENT=production`, enable provider gates, fill in
Discord IDs, rotate an existing PostgreSQL role, or authorize Google. Check
those items separately.

Real credentials live in `secrets/local/` and the ignored Hermes runtime in
`.runtime/hermes/`. Both are operationally sensitive. In particular,
`.runtime/hermes/.env` contains copied Discord and MCP tokens; do not print or
attach it.

Mode-`0600` credential files depend on container identity. Docket's UID/GID is
configured from the host by the production setup. Hermes defaults to UID/GID
1000. On a host whose credential owner is not compatible with the Hermes UID,
Hermes will fail to read its mounted files even though the paths exist. Check
numeric ownership and container UID before loosening permissions; do not solve
the problem with world-readable secrets.

`scripts/prepare-hermes-home.sh` preserves unrelated operator-managed Hermes settings:

* it creates `.runtime/hermes/config.yaml` from the template only when the
  active file does not already exist;
* it synchronizes Docket's managed MCP tool allowlist, the Discord platform
  toolset without generic cron delivery, and the four quiet-output display keys
  on every run;
* it rewrites `.runtime/hermes/.env` on every run.

Other template changes remain deliberately non-destructive. The sync preserves
unmanaged display keys, the CLI toolset (including CLI cron), and other active
configuration. Diff template changes explicitly before applying, restart Hermes
after active-config changes, and send `/reload-mcp` in existing sessions after a
tool/schema change.

Preparation rewrites the ignored Hermes `.env` from the credential files and
maps `DOCKET_OPERATOR_DISCORD_USER_ID` to Hermes' `DISCORD_ALLOWED_USERS`. It
does not carry forward `DISCORD_HOME_CHANNEL`. Compose repeats the operator
mapping explicitly so a container recreation cannot silently lose gateway
authorization. On Docket's Discord surface, background-process notifications
are disabled and `/sethome` is rejected in chat, queue, and system lanes. If
chat starts receiving unsolicited output, first check the effective
home-channel variables, the four managed display keys, and
`hermes cron list --all`; do not treat chat as a generic delivery target.

## Calendar cache and reminder recovery

Inspect only bounded metadata and deterministic notification state:

```bash
sudo docker compose exec -T postgres psql -U docket -d docket -x -c '
select account_id, calendar_id, window_start, window_end, status,
       last_attempt_at, last_success_at, last_error_code, leased_until
from calendar_sync_states;
select provider_event_id, status, is_all_day, start_at, start_date,
       recurring_event_id, synced_at
from calendar_event_cache order by coalesce(start_at, start_date::timestamp)
limit 50;
select id, scope, provider_event_id, lead_seconds, queue_channel_id, enabled, version
from reminder_rules order by updated_at desc limit 20;
select id, reminder_rule_id, provider_event_id, event_start_key,
       scheduled_for, daily_thread_id, status, attempt_count, last_error_code
from scheduled_notifications order by scheduled_for desc limit 50;'
```

First failure points:

* A sync in `syncing` past `leased_until` should be recovered by the worker. If
  it is not, inspect the worker heartbeat before altering the row.
* `stale` with a prior generation means Docket deliberately retained the last
  complete cache. Investigate `last_error_code`; never delete the generation or
  present it as fresh merely to clear the status.
* `failed` with no `last_success_at` means no complete snapshot has ever
  promoted. Check the read gate, OAuth status, exact Calendar ID, and Google
  response class.
* `missed_stale_calendar` means the event was first dispatchable only after it
  had begun. Docket intentionally did not emit a misleading on-time reminder.
* A notification in `delivering` is coupled to its outbox row. Recover/retry the
  outbox; do not create a second notification or post a manual copy.
* A reminder delivery retry verifies or unarchives the bound daily thread, then
  searches that thread for the exact
  `docket-calendar-reminder:<notification UUID>` footer. A foreign or duplicate
  marker is a security failure, not a reason to pick one arbitrarily.

## Google OAuth and calendar identifiers

The OAuth client must be a Google Desktop-app client. Docket's host-side setup
flow owns creation of `google_oauth_token.json`; do not hand-author it or copy
the smoke placeholder into the production directory.

```bash
uv run docket-google-auth status --credentials-dir secrets/local
scripts/setup-google-oauth.sh
```

The default approved bundle requests Calendar events, Gmail modify, Sheets,
and Docs together. Possessing those scopes does not expose corresponding MCP
tools. Provider actions remain limited by implemented Docket adapters and the
action registry. Gmail send/reply remains prohibited.

The setup requests offline access, requires a refresh token, strips the
short-lived access token before persistence, and writes the result with mode
`0600`. The credentials directory is mounted read-only into the current Docket
container. Future adapters must not assume they can persist a refreshed Google
token through that mount; add an explicit secure refresh-persistence design
before relying on runtime token rotation.

`GOOGLE_CALENDAR_ID` is an opaque Google calendar identifier. An ID ending in
`@group.calendar.google.com` is normal for a secondary/group calendar and must
be preserved exactly. Do not strip the suffix or replace it with a display
name.

`DOCKET_CALENDAR_READS_ENABLED` permits only bounded, paginated snapshots of the
configured Calendar. `DOCKET_EXTERNAL_WRITES_ENABLED` independently selects the
real mutation/reconciliation adapter. With writes disabled, approval and
operation smokes use the stateful in-process fake provider even if real reads
are enabled; a Calendar lookup can therefore never drain a pending write.

When either gate is enabled, Docket loads the authorized-user file and refreshes
an access token in memory. The read-only credential mount is sufficient because
the long-lived refresh token is already persisted and access-token refresh does
not need to rewrite the file. If Google rotates or replaces the refresh token,
rerun the host OAuth setup deliberately.

Before changing either gate, confirm the fake crash-window and snapshot suite:

```bash
uv run pytest \
  tests/integration/test_calendar_operations.py \
  tests/integration/test_schedule_workflow.py \
  tests/integration/test_calendar_read_model.py
```

After an action attempt, inspect bounded operational state without dumping
provider bodies or credentials:

```bash
sudo docker compose exec -T postgres psql -U docket -d docket -x -c '
select id, operation_type, status, attempt_count, next_attempt_at,
       last_error_code, provider_correlation
from operations order by created_at desc limit 10;
select operation_id, attempt_number, kind, status, error_code,
       provider_request_id, started_at, completed_at
from execution_attempts order by started_at desc limit 20;
select record_id, meeting_id, calendar_id, external_event_id,
       last_synced_version, updated_at
from calendar_links order by updated_at desc limit 10;'
```

Never manually change a `reconciliation_required` operation to `pending` while
the external outcome is unknown. Exactly one correlation match is linked;
zero matches wait through the consistency window before the same operation is
retried; multiple or mismatched results remain visible and emit a system-alert
outbox event.

## Private SearXNG routing

The expected URL from Hermes is:

```text
http://searxng:8080
```

It is a Compose-network address, not a host URL. SearXNG publishes no host port;
the Compose network still permits outbound access so SearXNG can reach search
engines. Limiting is disabled only because the service is network-private.

First checks:

```bash
sudo docker compose ps searxng
sudo docker compose exec -T hermes python - <<'PY'
import urllib.request
with urllib.request.urlopen("http://searxng:8080/healthz", timeout=5) as response:
    print(response.status)
PY
```

Expect HTTP 200. If health passes but Hermes search fails, check that the active
Hermes configuration selects the `searxng` backend and that `SEARXNG_URL` is
present without dumping the rest of `.runtime/hermes/.env`.

Do not publish port 8080 without adding authenticated ingress and revisiting
the limiter/public-instance settings. The SearXNG secret is delivered as a
Compose secret, not an environment value.

## PostgreSQL password mismatch

`POSTGRES_PASSWORD` initializes the `docket` role only when the PostgreSQL
volume is first created. Changing `.env` does not rotate the role password in an
existing volume.

If Docket reports authentication failure after credential generation:

1. Confirm the volume already existed.
2. Rotate the existing `docket` role password to the generated value without
   printing it.
3. Recreate Docket with the matching `DOCKET_DATABASE_URL`.
4. Do not delete the volume as a shortcut; that destroys durable state.

## Full verification

Before declaring the stack healthy after a change:

```bash
uv run pytest
uv run ruff check .
uv run mypy
sudo docker compose ps
sudo docker compose exec -T hermes hermes plugins list --plain --no-bundled
sudo docker compose exec -T hermes hermes mcp test docket
```

For Milestone 3 queue/lifecycle changes, also run:

```bash
uv run pytest \
  tests/unit/test_queue_lifecycle.py \
  tests/integration/test_queue_rollover.py \
  tests/integration/test_system_alerts.py \
  tests/adversarial/test_plugin_actor_gate.py
```

For Milestone 3.5 Calendar cache/reminder changes, also run:

```bash
uv run pytest \
  tests/unit/test_calendar_snapshot_provider.py \
  tests/integration/test_calendar_read_model.py \
  tests/adversarial/test_plugin_actor_gate.py
```

For changes to manual Discord persistence, additionally require one real
Discord remember request, durable source/audit/command evidence, an exact
replay, an unauthenticated 401, and forged-source rejection.

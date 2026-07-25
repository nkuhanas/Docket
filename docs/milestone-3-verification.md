# Milestone 3 verification

This record covers the durable daily queue lifecycle implemented and deployed
on 2026-07-22. The private specification remains outside this repository.

## Verified contract

| Contract | Evidence |
| --- | --- |
| One rollover per local ISO day | `system:daily_rollover:YYYY-MM-DD` is a unique durable command; concurrent insert races converge through a savepoint. |
| One item projection per day | A queue item and daily thread have one projection identity and a date-scoped outbox deduplication key. Restart replay reused both the fake thread and card. |
| Current controls only | Carryover tests prove the old card loses controls after the new card is acknowledged; approval binding moves to the new projection. |
| Authenticated local actions | Snooze/Ignore tokens bind revision, projection, queue version, and expiry. Callback validation also binds operator, guild, parent, thread, message, and interaction replay key. |
| Snooze wake semantics | A local-date snooze wakes at 07:00 in `America/Los_Angeles`; the DST fallback case resolves to 15:00 UTC and resumes once. |
| Archival lifecycle | Past threads archive after pending card delivery, unarchive for a historical edit, and rearchive without new thread/card identities. |
| Approval carryover | Expired approval state is retired; an unchanged eligible Calendar action receives a fresh immutable revision and approval. Old controls reject as stale. |
| Projection failure isolation | Exhaustion preserves canonical state and creates one durable system-channel alert. Lost acknowledgement recovery uses a stable alert marker. |

## Automated verification

The release candidate passed:

```text
uv run ruff check src tests hermes/plugin/docket_discord
uv run mypy src/docket
uv run pytest -q

103 passed, 1 dependency deprecation warning
```

The warning is Starlette's notice that its current `TestClient` HTTPX adapter is
deprecated; it is not a Docket failure.

Migration `0005` adds durable daily-thread lifecycle versioning. Hermes plugin
`0.4.0` adds ISO thread lifecycle, structured queue cards, Snooze/Ignore button
callbacks, and the separately allowlisted system-alert endpoint. Docket exposes
twelve MCP tools after reload, including four queue tools.

## Live deployment evidence

The deployed Docket container applied migration `0005`, became healthy, and
loaded a projection retry limit of ten. An unauthenticated cross-container
request reached Hermes port 8787 and returned HTTP 401, proving both network
reachability and bearer enforcement without reading a token.

The deployment restart also exercised a real recovery path. Docket claimed a
past-thread archival event while Hermes was unavailable. The original event
recorded five transport failures and produced one durable system alert. After
the listener repair, the alert delivered, the original lifecycle event was
requeued without resetting its attempt count, and attempt six archived the
same stored Discord thread. Final live state contained one active current-day
thread, one archived prior-day thread, and only delivered Discord outbox events.

## Live operator smoke

After `/reload-mcp`, Hermes reported the expected twelve Docket tools. A
provider-safe synthetic item was projected into the current ISO-day thread as
Discord message `1529421083315404871`. The operator pressed **Ignore** once.
Docket accepted exactly one authenticated `discord_local_action` command and
one plugin-attributed `queue_item.ignored` audit event. The canonical item moved
from `pending` version 1 to `ignored` version 2 with resolution
`operator_ignored`.

The original projection and its refresh both delivered on their first attempt.
The same Discord message advanced to projection version 2; its Ignore action
succeeded and its sibling Snooze action became `superseded`, so the refreshed
card has no current controls. No provider operation was created, and external
calls remained disabled throughout the smoke.

A later day boundary can add live evidence for carryover. Automated tests
already cover interaction replay, copied-card and forged-context rejection,
restart stability, stale-control retirement, archival, carryover, and
projection-failure handling without risking a real provider write.

## Operator thread-membership closure

On 2026-07-24, plugin `0.10.0` made configured-operator membership part of the
trusted daily-thread ensure contract. Docket sends only its configured operator
ID; the plugin independently compares that value with its own configured
identity, activates the exact bot-owned thread, and calls Discord's idempotent
thread-member endpoint before acknowledging. Docket accepts the acknowledgement
only when the same operator ID is echoed with `operator_joined=true`.

The full suite passed with 208 tests plus repository-wide lint and strict source
typing. Live deployment recreated Docket and Hermes, loaded plugin `0.10.0`,
reconnected Yuuka to Discord, and retained the private listener on port 8787.
A durable refresh then re-ensured the existing July 24 thread and joined the
configured operator on its first attempt. The stored thread, projection, card,
and Discord message identities were reused; the thread verification timestamp
advanced, no replacement card was necessary, and no operation or outbox work
remained pending.

Membership lets Discord clients discover the thread and maintain per-thread
read/notification state. Docket does not and cannot override the operator's
personal server, channel, thread, or device notification settings.

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

## Remaining operator smoke

Hermes caches MCP tools in the active session. Send `/reload-mcp` before the
Milestone 3 smoke and confirm that twelve Docket tools are reported. Then use a
fresh synthetic queue item to verify one real Snooze or Ignore button, that the
card refreshes without active sibling controls, and that the same interaction
cannot be replayed. A later day boundary can verify live carryover; automated
tests already cover restart, stale-control, archival, and projection-failure
cases without risking a real provider write.

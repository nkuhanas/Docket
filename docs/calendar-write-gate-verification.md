# Production Calendar write-gate verification

Verified on 2026-07-24 against commit `d643a27`.

## Contract

When `DOCKET_ENVIRONMENT=production` and
`DOCKET_EXTERNAL_WRITES_ENABLED=false`:

* `/health/ready` reports `calendar_write_mode=disabled`.
* Approve fails with `external_writes_disabled`, leaves the approval pending,
  and creates no operation.
* the operation worker does not claim pending or reconciliation work.
* the production provider boundary fails closed instead of returning fake event
  identifiers.
* Reject still validates the trusted actor, exact projection/message, current
  immutable revision, hashes, and queue identity, but does not require mutable
  Calendar, account, record, or queue-version freshness.

Smoke, development, and test environments retain the stateful fake provider for
explicit non-production verification.

## Automated verification

```text
uv run ruff check .  -> clean
uv run mypy          -> clean (62 source files)
uv run pytest -q     -> 204 passed, 1 dependency deprecation warning
```

Regression coverage includes the production provider selection, approval gate
state preservation, paused operation claiming, changed-record rejection, and a
schedule rejection after Calendar snapshot advancement.

## Deployed verification

Docket was rebuilt and force-recreated without changing the production write
permission. Readiness reported:

```text
status=ok
worker=ready
google_oauth=configured
calendar_reads_enabled=true
external_writes_enabled=false
calendar_write_mode=disabled
calendar_sync.status=current
calendar_sync.stale=false
```

The existing aggregate schedule card remained in its delivered Decision view.
Its approval remained pending, its queue item remained `awaiting_approval`, and
it had zero operations. There were no globally pending, running, or
reconciliation-required operations after deployment.

The operator can now press **Reject** on that card without refreshing it. If the
operator instead wants to execute the schedule later, enable external writes,
recreate Docket, use **Refresh** to bind a current Calendar snapshot, review the
replacement revision, and only then press **Approve**.

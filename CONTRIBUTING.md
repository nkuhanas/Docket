# Contributing to Docket

Docket is currently deployed as a credential-bearing, single-operator service.
Repository automation therefore proves an artifact is eligible for deployment;
it does not receive production credentials or mutate the live stack.

## Change workflow

1. Create a short-lived branch from `main`.
2. Keep private implementation provenance outside Git: versioned specifications
   belong under `specs/`, and dated change handoffs belong under `deltas/`.
   Both directories, along with `.env`, `secrets/local/`, and `.runtime/`, must
   remain untracked; do not place specifications or deltas under `docs/`.
3. Run `scripts/docket check`.
4. Run `scripts/docket compose-smoke` for changes to migrations, dependencies,
   Compose, health, MCP, authentication, or tool schemas.
5. Open a pull request and wait for both required CI jobs.
6. Merge only green source. Deployment remains an explicit operator action on
   the configured host through `scripts/docket deploy`.

Live Google Calendar and Discord smokes are operator-present verification. They
must never run in GitHub-hosted CI.

## Commit messages

Use:

```text
type(scope): imperative lowercase description
```

Accepted types are `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`,
`ci`, and `chore`. Choose a stable subsystem for the scope, such as `calendar`,
`discord`, `mcp`, `hermes`, `ops`, or `repo`.

Examples:

```text
feat(calendar): add standalone event proposals
fix(discord): preserve daily thread membership
ci(repo): run the isolated Compose smoke
```

Keep each commit coherent and include its tests and operational documentation.
Use `!` plus a `BREAKING CHANGE:` footer only for an intentional incompatible
contract change.

## Deployment boundary

`scripts/docket deploy` deliberately fails unless:

- the worktree is clean and on `main`;
- `HEAD` exactly matches `origin/main`;
- CI succeeded for that exact commit;
- the configured environment is `production`;
- no provider call, model turn, cron execution, or outbox delivery is currently
  claimed/in flight.

The command prepares the ignored backup directory for the invoking operator,
creates a durable drain barrier, waits for pre-barrier execution leases, creates
a PostgreSQL custom-format backup, retains the previous image under a
timestamped rollback tag, applies migrations, and replaces Docket and Hermes
while the restricted Discord ingress remains connected. It then verifies
Docket health, Alembic head, the Hermes gateway, the pinned plugin version, the
22-tool MCP registry, the private projection listener, and zero in-flight work.
Queued Operations, reconciliation work, and unclaimed outbox entries survive
the restart and do not block it.

The stable Discord ingress is deliberately excluded from normal deployment.
Deploy ingress-only code with `scripts/docket deploy-ingress`; that path drains
execution, removes all mutation-authorizing semantic controls, overlaps a
temporary append-only ingress writer, replaces the primary writer, and
regenerates exact persisted options before releasing the drain. Do not recreate
the ingress ad hoc with Compose because that would reopen an undefined Discord
receipt interval.

An image rollback does not reverse a database migration. Use the backup and the
migration-specific recovery procedure rather than blindly retagging an older
image.

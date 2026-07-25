# Contributing to Docket

Docket is currently deployed as a credential-bearing, single-operator service.
Repository automation therefore proves an artifact is eligible for deployment;
it does not receive production credentials or mutate the live stack.

## Change workflow

1. Create a short-lived branch from `main`.
2. Keep the private implementation specifications outside Git. Files matching
   `spec-*.md`, `.env`, `secrets/local/`, and `.runtime/` must remain untracked.
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
- no operation is pending, running, or awaiting reconciliation;
- no outbox delivery is pending.

The command prepares the ignored backup directory for the invoking operator,
creates a PostgreSQL custom-format backup before rebuilding, retains the
previous image under a timestamped rollback tag, applies migrations through
Docket startup, and verifies Docket health, Alembic head, the Hermes gateway,
the pinned plugin version, the 20-tool MCP registry, the private projection
listener, and drained durable work.

An image rollback does not reverse a database migration. Use the backup and the
migration-specific recovery procedure rather than blindly retagging an older
image.

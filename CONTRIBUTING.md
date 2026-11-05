# Contributing to Docket

[SPEC.md](SPEC.md) is the current specification. Use dated forward deltas and
`deltas/INDEX.md` to propose/adopt changes; reconcile SPEC decisions/history in
the same change. Older private provenance sources remain ignored and immutable.
Procedures here implement the SPEC rather than establish another specification.

## Change workflow

Operator development instructions authorize scoped work. Standing release
authorization includes branches, PRs, locally validated merges and related deploys;
no Discord, ledger or repeated approval step. Destructive reset/restore and
unrelated external/provider actions remain outside that authorization.

1. Create a short-lived branch and preserve unrelated working-tree changes.
2. Implement coherent slices and relevant tests/procedures. Review staged diffs.
3. Run `scripts/docket check` for native behavior, Ruff and strict mypy.
4. For affected areas use `scripts/docket check --extended provider`, `storage`
   or `release`. `all` is available for deliberate broad investigation.
5. Commit completed slices. Run `scripts/docket validate-release` on clean
   committed source before publishing/merging a release. It runs automatically
   selected checks from a tracked archive, builds an image and tests that exact
   artifact on disposable PostgreSQL. Storage changes include migration and
   focused concurrency checks. No production credentials are used.
6. Push reviewed source, open/merge its PR after local validation. GitHub Actions
   is optional manual diagnostics, never a required status wait. History
   protections remain. A content-identical merge/rebase reuses local evidence.
7. Synchronize clean `main` with `origin/main`; run `scripts/docket predeploy`
   then `scripts/docket deploy`. Routine related deployment needs no new approval.

`scripts/docket verify-release` verifies local evidence for the current tree.
Evidence in ignored `.runtime/validation/` records tree/source digest, lockfile,
check-policy/checker identity, successful groups and immutable image digest.
Changed content/missing checks/different image reject. Do not hand-edit evidence
or treat a development build as validated. If the image/evidence is missing,
rerun `validate-release`; a changed commit SHA alone is not invalidation.

Documentation/skill iteration needs relevant syntax/link/package checks, without
an unconditional database rebuild. Producing a deployable image still requires
its source-bound release validation. External provider acceptance uses explicit
operator-authorized targets, never synthetic success in production.

## Commits

Use `type(scope): imperative lowercase description`, with types `feat`, `fix`,
`perf`, `refactor`, `test`, `docs`, `build`, `ci` or `chore`, and precise scopes
such as `calendar`, `mcp`, `authority`, `ops`, `verification` or `repo`.
Each commit is coherent and includes necessary tests/docs. Do not rewrite pushed
history. Use `!` and a `BREAKING CHANGE:` footer for intentional wire breakage.

## Deployment and recovery

The supported path consumes the tested immutable image without rebuilding it.
It requires clean synchronized main, production settings, current authentication
and valid local evidence. It retains the prior image, creates a durable drain,
waits for pre-barrier executions and in-flight work, makes a PostgreSQL backup,
upgrades schema, recreates Docket and verifies health/schema/OAuth readiness.
Queued operations/reconciliation/notifications survive. Failed checks release
the drain without killing active work. Retired gateway containers are stopped
only after drain/backup; their volumes and historical evidence are preserved.

Hermes, SearXNG and Discord gateway deployments/cron are retired. Inference and
scheduling belong to the connected MCP client. The old one-time destructive
clean-cutover CLI is retired; its signed artifacts remain historical evidence.
Use `backup`/`verify-restore` and the migration-specific recovery procedure.
An image rollback does not reverse a database migration. Never restore/reset
or edit production rows merely to make a release check pass.

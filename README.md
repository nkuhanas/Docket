# Docket

Docket is a single-operator personal operations service with a native OAuth MCP
plugin at `https://docket.you/mcp/`. PostgreSQL retains canonical state,
provenance, triage and provider-operation intent. Connected agents supply
inference and scheduling; Google OAuth connects Calendar and Gmail.

[**SPEC.md**](SPEC.md) is the single current specification. Dated adopted deltas
and architecture decisions explain changes without becoming competing specs.
The context/flat-command transition is in progress; see
[implementation evidence](docs/native-context-local-ci.md).

## Development and operations

Read [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md) and
[documentation routes](docs/README.md) before making changes.

```bash
scripts/docket check
scripts/docket check --extended provider  # affected provider work
scripts/docket check --extended storage   # affected durable-model work
scripts/docket check --extended release   # affected lifecycle work
scripts/docket compose-smoke              # native MCP on disposable PostgreSQL
```

The fast gate runs native safety cases, Ruff and strict mypy. There is no
mandatory GitHub Actions run. `validate-release` uses a tracked-source archive,
automatically selects affected checks and records evidence for its tested image.

```bash
scripts/docket validate-release
# Publish/merge reviewed content; synchronize clean main with origin/main.
scripts/docket predeploy
scripts/docket deploy
```

Deployment consumes the exact tested image, drains durable execution, backs up,
upgrades schema and verifies health/OAuth readiness. An image rollback cannot
reverse a migration; follow migration-specific recovery and verified backups.
Never use production credentials for tests. `.env.example` and `secrets/smoke/`
contain synthetic settings only. Production setup is documented in
[plugin setup](docs/plugin-setup.md) and the [runbook](docs/operations-runbook.md).
For Google authorization use `scripts/setup-google-oauth.sh`; do not expose the
printed authorization callback or credential files.

## Repository

| Location | Purpose |
| --- | --- |
| `SPEC.md`, `deltas/INDEX.md` | Current requirements, decisions and forward history |
| `src/docket/plugin/`, `plugin/docket/` | Native OAuth MCP boundary and plugin package |
| `src/docket/services/`, `models/`, `schemas/` | Transactions, durable state and wire contracts |
| `src/docket/providers/`, `worker/` | Provider projection and asynchronous execution |
| `migrations/clean_versions/` | Ordered PostgreSQL schema history |
| `scripts/docket` | Supported local checks, release, backup and diagnosis |
| `tests/ci-policy.json` | Native and affected-area test groups |
| `docs/` | Procedures and verification evidence |

Hermes, SearXNG, gateway cron and browser-automation snapshots are retired.
Their history remains in Git; they are not native startup dependencies. Older
private `specs/` and deltas remain ignored and retain their original provenance.

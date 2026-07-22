# Docket

Docket is a durable authorization and state layer for personal operations. This
repository currently implements the Milestone 0-2 path: term/course persistence,
typed Calendar proposals, immutable previews, authenticated one-time approvals,
durable operations and attempts, Google Calendar create/update/reconciliation,
audit history, and Hermes integration. Detailed implementation specifications
are maintained privately and excluded from Git.

## Operational documentation

Start with [`docs/README.md`](docs/README.md). The
[operations runbook](docs/operations-runbook.md) contains symptom-first checks
and recovery procedures; the
[pinned integration contracts](docs/pinned-integration-contracts.md) record the
fragile Hermes, MCP, Compose, and container assumptions that must be revalidated
before upgrades.

## Safety defaults

The checked-in smoke configuration is intentionally fake:

* `DOCKET_EXTERNAL_CALLS_ENABLED=false`
* fake Google OAuth files
* fake Discord and service tokens
* the Hermes service is behind the optional `hermes` Compose profile

With this switch false, Calendar operations use the stateful fake adapter and
cannot contact Google. The Hermes profile remains opt-in, so the basic smoke
also cannot contact Discord.

## Quick smoke

```bash
test -f .env || cp .env.example .env
uv sync
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run mypy
docker compose up --build -d postgres docket
uv run python scripts/compose-mcp-smoke.py
```

Compose publishes Docket only on `127.0.0.1:8000`; it is not exposed on the
host's external interfaces. The Docket container applies Alembic migrations
before starting the API. The MCP smoke uses the checked-in dummy service token
and a fake provider, and makes no Discord or Google calls.

If Docker reports permission denied for `/var/run/docker.sock`, prefix the
Compose commands with `sudo`; that is required by this host's current group
configuration.

Do not run the Compose smoke against a configured production `.env`; it is
deliberately restricted to the checked-in dummy credential set.

## Private search for Hermes

SearXNG is included as a network-private Compose service with JSON results
enabled. It has outbound web access for searches, but no host port is published.
Generate its production secret and start it with:

```bash
uv run docket-production-config
sudo docker compose up -d searxng
```

Containers on Docket's Compose network can use `http://searxng:8080`. The JSON
API endpoint is `/search?q=QUERY&format=json`. Do not publish port 8080 without
adding an authenticated reverse proxy and revisiting the limiter settings.

## Real credentials and Google authorization

Read [`secrets/README.md`](secrets/README.md), place the operator-supplied files
in `secrets/local/`, and set `DOCKET_CREDENTIALS_DIR=./secrets/local` in `.env`.
Files under `secrets/local/` are ignored by Git. Do not create
`google_oauth_token.json` yourself: Docket owns and generates that file.

After placing a Google Desktop-app OAuth client at
`secrets/local/google_oauth_client.json`, run:

```bash
uv run docket-google-auth status --credentials-dir secrets/local
scripts/setup-google-oauth.sh
```

The setup defaults to the operator-approved Workspace bundle (Calendar events,
Gmail modification, Sheets, and Docs), opens the Google consent page, requests
offline access, and atomically writes
`secrets/local/google_oauth_token.json` with mode `0600`. It never enables
external calls. A missing token is reported as `google_oauth=setup_required` by
`/health/ready`; startup and dummy smokes remain non-interactive.

Authorization does not expand Docket's tool surface. The token remains
Docket-only, Sheets/Docs adapters are not implemented, and Gmail send/reply is
still prohibited. The deliberate scope expansion is recorded in
[`docs/deviations.md`](docs/deviations.md).

Generate the production database and SearXNG credentials without displaying
them, then render the ignored Hermes runtime from the configured Discord IDs:

```bash
uv run docket-production-config
scripts/prepare-hermes-home.sh
```

The production command atomically updates `.env`, stores the database credential
at `secrets/local/postgres_password`, creates `secrets/local/searxng_secret`, and
configures the Docket container UID/GID to read mode-`0600` secrets. A fresh
PostgreSQL volume consumes its credential during initialization. An existing
volume requires its `docket` role password to be rotated to the same value before
Docket starts.

Hermes stores OpenAI OAuth and Discord runtime configuration in
`.runtime/hermes/`, which is also ignored. Run `scripts/prepare-hermes-home.sh`
after placing the Discord token and IDs, then use the official interactive setup
for OpenAI OAuth:

```bash
sudo docker compose --profile hermes run --rm hermes setup
```

Compose mounts the interactive `docket-manual-intent` skill into Hermes's normal
skill index. The restricted Gmail triage skill remains plugin-namespaced and is
not exposed to ordinary Discord sessions.

Do not enable external calls until the fake-adapter suite passes and an operator
is present for the separately controlled real-account smoke. Automated tests
never load the production OAuth credential or calendar ID.

## Hermes pin

* Release tag: `v2026.7.20`
* Source commit: `c7d08de287556b3d339df336b180a39d4980ebd7`
* Image: `nousresearch/hermes-agent:v2026.7.20`

The tag, commit, image, MCP support, plugin discovery, and
`pre_gateway_dispatch` hook were verified against official Hermes sources on
2026-07-21.

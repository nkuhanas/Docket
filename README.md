# Docket

Docket is a durable authorization and state layer for personal operations. This
repository currently implements the Milestone 0-3.5 path: term/course persistence,
typed Calendar proposals, immutable previews, authenticated one-time approvals,
durable operations and attempts, Google Calendar create/update/reconciliation,
audit history, durable Discord daily-thread/card projection, and Hermes
integration. Milestone 3 adds canonical queue reads and local transitions,
07:00 local daily rollover, carryover with one current control surface, thread
archival recovery, and durable system-channel failure reporting. Detailed
implementation specifications are maintained privately and excluded from Git.
Milestone 3.5 adds a bounded, atomically promoted Calendar read model, freshness
reporting, explicit reminder rules, and deterministic deduplicated Discord reminders.
Calendar lookups resolve `today` and `tomorrow` inside Docket's configured
timezone, so Hermes does not need terminal access to derive local-day bounds.

## Operational documentation

Start with [`docs/README.md`](docs/README.md). The
[operations runbook](docs/operations-runbook.md) contains symptom-first checks
and recovery procedures; the
[pinned integration contracts](docs/pinned-integration-contracts.md) record the
fragile Hermes, MCP, Compose, and container assumptions that must be revalidated
before upgrades.

## Safety defaults

The checked-in smoke configuration is intentionally fake:

* `DOCKET_CALENDAR_READS_ENABLED=false`
* `DOCKET_EXTERNAL_WRITES_ENABLED=false`
* fake Google OAuth files
* fake Discord and service tokens
* the Hermes service is behind the optional `hermes` Compose profile

In smoke, development, and test environments, both false gates select stateful
fake adapters that cannot contact Google. In production, a false external-write
gate pauses operation claiming and selects a fail-closed provider; production
never records fake Calendar success. The Hermes profile remains opt-in, so the
basic smoke also cannot contact Discord.

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

Do not enable Calendar reads or external writes until the fake-adapter suite
passes and an operator is present for the separately controlled real-account
smoke. The gates are independent: enabling bounded Calendar synchronization
does not activate approved provider writes. Automated tests never load the
production OAuth credential or calendar ID.

## Calendar cache and reminders

Docket owns the rolling Calendar snapshot; Hermes never receives a raw Google
client. The cache retains the last complete generation when pagination or
authorization fails, and every lookup reports coverage and freshness. Real
snapshot calls require `DOCKET_CALENDAR_READS_ENABLED=true`. Approved Calendar
mutations remain separately gated by `DOCKET_EXTERNAL_WRITES_ENABLED=true`.
The existing lookup accepts an explicit timezone-aware interval, a Docket-owned
`today`/`tomorrow` relative day, or no interval for its rolling seven-day
default. Relative results include the resolved local date, timezone, and server
`as_of` instant. Timed events include configured-local timestamps for direct
display. Current-day list/find requests use a bounded fresh refresh so a newly
created provider event is not hidden until the next periodic sync.

Reminder rules are created only by an explicit operator request. Delivery uses
a bounded deterministic embed in the reminder due date's ISO thread under
`DOCKET_QUEUE_CHANNEL_ID`. Docket creates, finds, or unarchives the thread and
binds the acknowledgement to its parent, thread, and message IDs. Rules cannot
choose a Discord destination or act as an arbitrary message-send surface.

## Hermes pin

* Release tag: `v2026.7.20`
* Image revision: `3ef6bbd201263d354fd83ec55b3c306ded2eb72a`
* Image: `nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a`

The tag, image identity, MCP support, plugin discovery, and pinned gateway seams
were verified against the deployed Hermes image on 2026-07-22.

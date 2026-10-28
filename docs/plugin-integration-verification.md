# Native plugin integration

The Operator authorized local implementation of the October 28 primary-plugin
delta in the development conversation. Production, identity-provider and network
configuration, plugin installation and live domain actions remain separate steps.

The first slice adds explicitly approved OAuth principals, JWT verification and
transport-independent Operator binding. Login alone cannot enroll a user. The
configured issuer/JWKS, API audience, predefined client, dates and scopes are
checked before the local approved-owner record. Principal identity is retained
across reconnect/reapproval; revocation and a new grant epoch reject old tokens.
Neither tokens nor provider subject claims enter receipts or audit payloads.

All OAuth principals are currently read-only, optionally with notification
subscription permission. OAuth write scopes, copied request metadata and connected
background tasks cannot admit interactive work. No browser-confirmation fallback
or prompt-only foreground classification has been implemented. The host execution
contract remains a gate before native canonical writes can be enabled.

Migration `20261028a1b2` adds the principal registry and PostgreSQL identity guards
without rewriting historical records. Downgrade refuses to discard enrolled
principal history. Existing deployments retain their historical Operator identity
by default; `DOCKET_OPERATOR_REF` explicitly selects an identity independently of
Discord. A configured identity cannot reassign a retained principal to a new owner.

`docket-plugin-auth approve --subject '<provider user subject>' --events` previews
administration. Add `--execute` only for an explicitly authorized runtime mutation;
retain the returned public principal reference for revocation. The command does
not sign into Auth0, issue tokens or change Google's authorization.

OAuth slice validation: `scripts/docket check` passed 1,026 tests, Ruff and strict
mypy across 163 source files. `scripts/docket compose-smoke` passed the existing
HTTP workflows, governance/restore and concurrency checks, plus PostgreSQL-only
principal identity update/delete rejection, explicit revocation, retained-history
downgrade refusal and empty migration downgrade/re-upgrade. Synthetic credentials
and isolated databases were used. No native deployment or private linking has run.

The transport now pins MCP SDK 2.3.0. The guarded adapter retains admission,
exact-revision binding, compact results, rejected-call audit and provider recovery
while producing typed MCP results. Actual ASGI sessions test both legacy
`2025-11-25` and modern `2026-07-28`, including default/hard page caps and schema
rejection. `scripts/docket check` passed 1,028 tests, Ruff and strict mypy;
`scripts/docket compose-smoke` passed the genuine HTTP foreground workflow,
restricted ingress, governance restore, PostgreSQL concurrency and migrations.
The existing Hermes client protocol remains supported; live Hermes behavior is
a separate rollout observation.

The public native app is mounted privately under `/plugin`; the ingress example
maps only MCP, resource discovery and the minimal connection page to it. It
accepts approved OAuth users only, rejects legacy service credentials and ignores
foreground headers. Both wire protocols expose 18 bounded reads plus the stable
profile tool, with OAuth metadata and no gateway-only arguments. Attempted native
commits are audited and denied without admitting `req_`. The old 23-tool service
surface and four-tool triage profile remain separate.

Native-facade validation: the complete quality gate passed 1,032 tests, Ruff and
strict mypy across 166 source files; the isolated Compose smoke passed. Actual
ASGI account-linking tests verify schema/profile parity, public route isolation,
wrong credentials, revocation and copied foreground context. Candidate plugin
source and an Auth0/network handoff are prepared. The public hostname, provider
configuration, exported package and installed plugin remain unverified.

Migration `20261028b2c3` adds an immutable retained inbox, encrypted subscriptions,
leased deliveries and retained attempts. Notification creation shares the source
transaction. Attention revisions, briefs, reminders and operation status publish
generic summaries and public references; untrusted source text is absent from
events. The native profile now exposes 21 read tools, including inbox list/get.
Modern MCP discovery advertises the webhook Events extension; legacy tools remain
available without advertising unsupported legacy event methods.

Verification uses one-use signed callback challenges, deterministic subscription
identities, bounded verification caching and finite lifetimes. Renewal, secret
rotation, unlink/revocation, expiration and reapproval epochs preserve ownership.
Stopped subscriptions purge their encrypted configuration. Outbound HTTPS pins
validated public DNS addresses while preserving TLS hostname checks and rejects
redirects/private destinations. Callback secrets and URLs are excluded from audits
and routine error logs. An admitted send may finish after concurrent revocation.

Webhook acceptance is recorded separately from inbox availability and Operator
visibility. Transient failures retry with bounded backoff, stable event IDs/body
and fresh signatures; permanent failures retain inbox and attempt history. An
expired delivery lease records the interrupted attempt as unknown before replay.
This projection replay never retries a Google provider mutation. A separate worker
loop prevents callback outages from blocking provider execution.

PostgreSQL smoke verifies concurrent claim exclusion, commits before network I/O,
immutable inbox/subscription/delivery identities, immutable completed attempts and
retained-history downgrade refusal. Empty migrations downgrade and upgrade again.
The full gate additionally covers actual modern Events HTTP discovery,
subscribe/refresh/unsubscribe, filter matching, atomic rollback, pagination,
Standard Webhooks signatures, revoked access, restart recovery and Discord-free
reminder availability. No external callback or ChatGPT-hosted acceptance has run.

Notification slice validation: `scripts/docket check` passed 1,064 tests, Ruff
and strict mypy across 172 source files; `scripts/docket compose-smoke` passed.
The final signing-rotation review also passed 15 focused notification/protocol
tests, including refresh during the dual-signature window.

Native-only lifecycle selection now retains CI, drain, backup and migration
guards while removing gateway/ingress readiness requirements. An existing gateway
is drained and its controls quiesced before retirement; later native deployments
need neither Hermes tokens nor the ingress database password. The enabled-adapter
path remains available during overlap, including restricted triage cron. Switching
it off stops that cron, so replacement triage remains a cutover acceptance item.
Private postdeploy checks cover resource metadata, anonymous denial and isolation;
they do not claim public ingress or signed-in host acceptance.

Failure simulation exposed a pre-existing exit-handler scope bug after a backup
failure. Cleanup now captures the exact drain reference when installed. The test
verifies barrier abort without stopping or replacing adapters after failed backup.
Deployment idle counts include native delivery claims and tolerate a pre-migration
database. Lease recovery records unknown attempts and releases abandoned claims
without network I/O during drains. Missing encryption keys cannot be regenerated
over retained encrypted callback settings. Isolated native startup explicitly uses
nonexistent Hermes/ingress credential paths and starts/stops the actual application.

Final local lifecycle validation: `scripts/docket check` passed 1,075 tests,
Ruff and strict mypy across 172 source files. `scripts/docket compose-smoke`
passed the legacy workflow, PostgreSQL checks, empty migration round trip and
native-only startup. No production deployment, public DNS/tunnel change,
Auth0 enrollment or installed-plugin verification has occurred.

On October 28 the Operator selected the Native client and supplied
`H92evvENOutMDq3Bq1qyRzMOrHiL7qpB`. The existing tenant's public discovery and
signing keys were revalidated for the exact issuer/JWKS, public token exchange,
PKCE `S256` and RSA signing keys. The selected client is recorded in the setup
guide and an ignored candidate environment overlay. The Operator clarified that
the target retires Hermes entirely, including triage; the candidate disables the
adapter. Inference and scheduling will belong to the connected MCP client, using
Docket's inbox and restricted intelligence workflow. This records configuration
preparation only: no live
environment, approved-owner registry, callback, API grant or hosted connection
was changed. Application-specific settings remain unverified until linking.

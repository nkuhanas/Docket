# Private Docket plugin setup

This guide describes configuration and live acceptance for the native interface.
The Operator authorized retiring Hermes and Discord during the drained native
deployment. Public DNS/tunnel activation and signed-in plugin acceptance are
separate checks; a healthy private backend does not prove either. The November 4
amendment delegates full write capability to the approved ChatGPT connection,
including scheduled and event-triggered runs, with the separate `docket.write` grant.

The Operator selected the Native/public PKCE connection and supplied the Docket
client ID `H92evvENOutMDq3Bq1qyRzMOrHiL7qpB` on October 28, 2026. The existing
tenant is `dev-yo4e5wrtdnuusefg.us.auth0.com`. Its public discovery document and
JWKS were rechecked: the issuer matches, `none` and PKCE `S256` are advertised,
and RSA signing keys are available. This public check does not inspect the new
client's API grant, rotation setting, login connection or callback registration.

## Operator action: create the Docket OAuth connection

The existing Aphelion Auth0 tenant and owner can be reused. Create a separate API
and a separate application; Docket must not accept Aphelion's audience or client.

1. Under **Applications → APIs**, create `Docket` with identifier
   `https://docket.you`, signing algorithm `RS256`, permissions `docket.read`,
   `docket.write`, `docket.events` and `docket.triage`, and **Allow Offline Access** enabled.
2. Verify the tenant's **Resource Parameter Compatibility Profile**, as used by
   Aphelion, so a request with `resource=https://docket.you` targets this API.
3. Create a **Native** application named `Docket ChatGPT`, using public PKCE
   token exchange (`none`). Enable Authorization Code and Refresh Token grants,
   refresh-token rotation and the owner's login connection. A new Native
   application's public-client setting may not show a Credentials tab.
4. Give that application explicit **user-delegated** access to the Docket API
   for the four permissions above. Keep machine-to-machine access absent.
   When RBAC is enabled, assign the owner permissions so the token's `scope`
   claim contains the granted scopes.
5. Retain the new application client ID, issuer URL, JWKS URL and exact owner
   user subject. These are setup identifiers, not access/refresh tokens or a
   client secret. The existing tenant/owner identifiers are already documented
   in Aphelion; only the new Docket client/grants need creating.
6. When the verified server is connected in ChatGPT, register the **exact
   callback URI displayed by that connection** in Auth0. Do not guess it or use
   a wildcard. Complete linking and verify `docket_get_profile` in Dot's own task.

The ChatGPT connection uses the new predefined client ID, public token-endpoint
authentication (`none`) and scopes
`docket.read docket.write docket.events docket.triage`; it needs no client secret.
A restricted connection can omit write, events or triage independently.
Auth0 authenticates ChatGPT to Docket. Existing Google OAuth credentials remain
private to Docket's provider adapters and are not passed to the plugin or Auth0.

These provider-account steps need the Operator's signed-in Auth0 dashboard. No
credential extraction or management token is needed in chat.

## Private plugin and account linking

The portable package at `plugin/docket/` declares the existing registered Native
client in `mcp.json`: OAuth, registration mode `provided`, the Docket client ID,
and token-endpoint authentication `none`. This prevents the host from attempting
dynamic client registration. Discovery supplies the issuer and endpoints.
The four Docket scopes are requested with `offline_access`, which Auth0 requires
to issue a refresh token. That standard scope adds no Docket domain permission;
the local owner grant independently limits all four capabilities.

For hosted ChatGPT use, register the MCP connection before binding the workflow
package. In **Plugins → + → Add custom MCP server**, use
`https://docket.you/mcp/`, OAuth and the predefined public client above. Copy the
exact redirect URI shown there into **Applications → Docket ChatGPT → Settings
→ Allowed Callback URLs** in Auth0, then save, finish account linking with the
existing approved owner and select **Create as a plugin**. No client secret is
required. Do not replace unrelated callback entries or use a wildcard.

Retain the actual new ChatGPT connection URL and inspect its saved metadata to
obtain the verified App ID. Bind that existing connection through `.app.json`
and `extensions.com.openai.apps`, preserving the connection's identity. Do not
invent an App ID, derive one by rewriting a plugin-ID prefix, or upload a second
unbound plugin to work around missing registration. The bound hosted package
omits duplicate direct-MCP wiring. The portable `mcp.json` remains useful for
clients that consume it, but saving that file alone does not prove ChatGPT has
registered or exposed the tools. PolyPath's October 28 hosted verification
record documents this exact failure and its successful binding correction;
Aphelion uses the registered-connection pattern as well.

Auth0's public metadata currently advertises issuer identification. OpenAI
documents a stable callback for compliant issuers, but the connection's displayed
URI is the authoritative value to register. Verify `docket_get_profile` after
linking; full tool discovery, an actual triage result and hosted event delivery
remain separate live acceptance checks.

On November 4, independent public checks passed for resource discovery (correct
issuer and the then-configured three scopes), anonymous MCP denial and private-path
isolation. The node's resolver still fails for `docket.you`; verification used public DNS answers
with normal TLS hostname/certificate validation. This does not establish local
resolver recovery or signed-in plugin acceptance.

References: [OpenAI registered clients](https://developers.openai.com/plugins/build/plugins#use-a-registered-oauth-client),
[hosted connection setup](https://developers.openai.com/plugins/build/plugins#create-and-test-a-plugin-locally-with-an-mcp-server),
[OpenAI redirects](https://developers.openai.com/plugins/build/auth#redirect-url),
and [Auth0 refresh tokens](https://auth0.com/docs/secure/tokens/refresh-tokens/get-refresh-tokens).

## Authentication succeeds but tool discovery fails

The OAuth callback returns to ChatGPT; its authorization code is transient and
belongs to that host's PKCE flow. Diagnosis needs the displayed error and server
status, not a pasted callback query or token. A successful identity-provider login
does not establish that Docket accepted the subsequent access token.

Private runtime logs emit `plugin_authentication_rejected` with a fixed reason
code. A missing required claim also records its field name from a fixed allowlist.
The verifier accepts Auth0's signed `azp` client claim and the RFC 9068 signed
`client_id` claim. At least one is required; both must agree when present, and
the value must match the configured client allowlist. Neither format bypasses
the approved-owner or scope checks. See [Auth0 token profiles](https://auth0.com/docs/secure/tokens/access-tokens/access-token-profiles).
An approved token missing `docket.read` receives `403` with an
`insufficient_scope` challenge naming that required scope, enabling client
reauthorization. Other authentication failures retain the generic `401`.
No token, header, claim value, request body, subject or exception text enters this event.
Use its label to choose the affected configuration check:

| Reason | Check |
| --- | --- |
| `credential_missing` | Authorization forwarding and the host's saved connection. |
| `signing_keys_unavailable` / `signing_key_unknown` | Configured JWKS reachability and signing-key rotation. |
| `invalid_signature` / `invalid_issuer` / `invalid_audience` | Exact signing issuer, Docket API resource and audience. |
| `missing_claim` | The Docket API's token profile and the required field shown. |
| `client_not_allowed` | The saved host client must be the explicitly configured Docket Native client. |
| `client_identity_missing` / `client_identity_conflict` | A signed client identity is required and both supported claims must agree. |
| `read_scope_missing` | Requested scopes, the application's user-delegated grant and user RBAC. |
| `principal_not_approved` | Sign in as the enrolled owner, using the intended Auth0 login connection. |
| `principal_disabled` / `principal_stale` | Local grant status and a fresh link after reapproval. |
| `operator_mismatch` | Preserved local Operator binding; never rewrite historical identity to pass login. |
| `expired_token` / `invalid_token_time` | Host token refresh and issuer/server clock health. |

An unexpected account or client does not enroll itself. Correct that exact
connection or use explicit approved-owner administration; keep scope separation
and all token-verification checks enforced.

Scope names must match exactly. Generic `read`, `write` and `triage` permissions
do not satisfy `docket.read`, `docket.events` and `docket.triage`. Define the Docket
names, and `docket.write` for delegated writes, on its Auth0 API. Grant them to
the Native application's **User-Delegated Access**, and allow them through user
role assignments when RBAC is enabled.
Reconnect to obtain a newly scoped token; changing permission definitions does
not rewrite a token already issued. See [MCP scope challenges](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#runtime-insufficient-scope-errors).

## Prepared runtime configuration

The selected nonsecret settings are prepared below. Apply them through the
authorized deployment path after the account and network configuration exists.
The Operator selected a Hermes-free deployment, including retirement of its
triage cron. Inference and scheduling belong to the connected agent; Docket
retains its inbox, bounded triage workflow, canonical state and Google adapters.
The target configuration disables the old adapter:

```dotenv
DOCKET_PLUGIN_ENABLED=true
DOCKET_DISCORD_ADAPTER_ENABLED=false
DOCKET_INTERACTIVE_AGENT_ENABLED=false
DOCKET_PLUGIN_PUBLIC_ORIGIN=https://docket.you
DOCKET_PLUGIN_OAUTH_ISSUER=https://dev-yo4e5wrtdnuusefg.us.auth0.com/
DOCKET_PLUGIN_OAUTH_JWKS_URL=https://dev-yo4e5wrtdnuusefg.us.auth0.com/.well-known/jwks.json
DOCKET_PLUGIN_OAUTH_CLIENT_IDS=["H92evvENOutMDq3Bq1qyRzMOrHiL7qpB"]
DOCKET_PLUGIN_READ_SCOPE=docket.read
DOCKET_PLUGIN_WRITE_SCOPE=docket.write
DOCKET_PLUGIN_EVENTS_SCOPE=docket.events
DOCKET_PLUGIN_TRIAGE_SCOPE=docket.triage
```

The ignored `.runtime/plugin/native-oauth.env` contains this overlay and preserves
the current Operator identity explicitly. It is not loaded automatically; the
authorized deployment installs its values in production `.env`, retaining a
private backup. No OAuth client secret is needed. The exact callback URI comes
from the eventual ChatGPT connection, after the public endpoint is available.

Retain the current Operator identity when disconnecting Discord. Set
`DOCKET_OPERATOR_REF` explicitly to that same historical identity; do not invent
a new identity or rewrite prior rows to make account linking work. Enrollment
uses the configured Operator identity and issuer. The local administrative
command previews by default:

```bash
uv run docket-plugin-auth approve --subject '<exact owner subject>' --write --events --triage
```

Add `--execute` for the approved owner enrollment after migration, within the
standing release authorization in `AGENTS.md`. The returned `prn_` is the stable
public account identity and can be supplied to `docket-plugin-auth revoke`. Reapproval preserves
identity but invalidates older tokens; obtain a newly issued token afterwards.

## Network handoff

Match Aphelion's dedicated Cloudflare Tunnel → proxy Caddy → private mTLS backend
→ loopback API pattern, using Docket's own hostname and routing. The candidate
app-side allowlist is in
[`deploy/plugin-ingress.Caddyfile.example`](../deploy/plugin-ingress.Caddyfile.example).
Only MCP, protected-resource discovery and the minimal connection page are public.
Internal callbacks, triage, operational health details and OpenAPI stay private.

Preserve authorization headers, streaming responses and suitable idle timeouts.
Avoid an interactive Cloudflare Access/bot challenge in front of the MCP route;
OAuth enforces the application account boundary. A tunnel alone grants no access.
Review the actual listener, certificates, firewall, DNS and existing proxy
services before applying the example. Runtime deployment receipts and the exact
network activation handoff are retained privately under `.runtime/plugin/`.

## Agent-supplied triage

Docket supplies no inference provider or model scheduler. A connected Dot, an
agent-managed cron job, or a local inference client can run the same bounded
workflow. OAuth clients need both `docket.read` and `docket.triage`, plus local
owner approval with `--triage`. API scopes alone do not approve an owner.

Subscribe to `docket.triage.ready`, poll `docket_list_notifications`, or call
`docket_get_triage_context` from a client-owned schedule. Each context call claims
one eligible source. Pass its exact run/context/source references and claim token
to `docket_submit_triage_analysis`, or apply an existing matching suppression with
`docket_apply_existing_suppression`. Stop when the context returns `no_sources`.
The client chooses its model, schedule and per-run work budget. Clients supporting
legacy MCP can poll; webhook Events require the modern protocol.

Claims expire and can be reclaimed. OAuth claims bind the approved principal and
claim generation: a later claim cannot be completed with an older context, and a
different principal cannot borrow a claim. Concurrent completion serializes.
Ready-work inbox entries cover backlog and expired claims without launching a
model. Webhooks contain generic metadata and refs, never Gmail bodies.

Triage tools create intelligence, apply existing Preferences and publish cases or
briefs. They cannot mutate canonical state or Google providers. An agent separately
granted `docket.write` can follow that analysis with the canonical workflow below.
OAuth `docket.events` only manages subscriptions.
Local clients can also use the private four-tool `/triage-mcp/` service credential,
independently of Hermes. The client manages its own credentials and refresh flow.

## Delegated canonical writes

The Operator explicitly authorized full write authority for the approved ChatGPT
connection, including scheduled and event-triggered runs. This replaces the earlier
foreground-only gate. Docket authenticates the delegated agent; it does not claim
that MCP session metadata proves the Operator is present. Legacy triage/read-only
credentials retain their limits.

Write access requires both `docket.write` in the signed OAuth token and a matching
local owner grant. Upgrade the already-approved owner through its public reference:

```bash
docket-plugin-auth approve --principal-ref prn_... --write --events --triage --execute
```

Reapproval preserves the principal, records its permissions and invalidates older
tokens/subscriptions. Reconnect once after the grant change and refresh subscriptions.
Revoking the principal or removing its write grant stops later calls; an already
admitted provider operation retains its original durable authority and recovery rules.

The agent supplies `work={request_id,execution_id,operation_id}` on each write tool.
These are bounded opaque IDs, never transcript text or proof of human presence.
Keep a request ID for one task and its repairs; keep an execution ID for one run.
Retry an unchanged tool operation with the same operation ID; changed arguments need
a new operation ID. IDs do not grant permission, and another principal cannot reuse
them to obtain the original request. Docket derives the owned `req_` and internal
operation binding. There is no separate begin call for the draft.

Use `docket_stage_changes`, optional `docket_review_changeset`, then
`docket_commit_changeset`. Commit takes work correlation only, never a mutation
payload or client-selected revision. A resumed run with a new execution ID must
review before committing; an execution cannot commit a newer unseen draft.
Clarifications and conflicts use the existing bounded tools and shared service.

`docket_record_context` optionally retains encrypted reported transcript, response
or source context in a separate transaction. A capture failure does not invalidate
the request or block its stage/commit. A report remains agent-reported; missing
attachment archives do not become fabricated `utt_` or `src_` evidence. Canonical
commit and Google delivery remain distinct outcomes.

## Acceptance before cutover

The native registry has 30 tools: 21 reads, three bounded triage tools, five
canonical workflow tools and optional context capture. Owners without local write
approval discover the existing 24-tool surface. Write-approved owners discover
the six additional tools even when their token needs a scope upgrade; those calls
remain denied until a valid `docket.write` token arrives, with a tool-level OAuth
challenge to prompt reauthorization. Discovery eligibility grants no permission.
Six modern MCP Events
cover ready triage work, attention revisions, published briefs, due reminders,
provider-operation status and operational alerts. Events carry
generic summaries and public references; use the bounded read tools for context.
Subscriptions are notification permissions and grant no canonical authority.

Callback verification and event delivery require outbound HTTPS. Subscriptions
last one day by default, with a one-minute minimum and seven-day maximum. Refresh
uses the same deterministic subscription identity. Successful callback proof is
cached for at most five minutes. Signing-key replacement has a 60-second dual
signature window. Unsubscribe, expiration and locally revoked grants stop new
claims and purge the encrypted callback configuration; an already claimed send
may finish. Reapproval invalidates old subscriptions until refreshed.

The existing attachment encryption key also derives the event-configuration
key using a distinct context. Retain it with credential backups. Replacing it
without migration makes retained callback configurations unreadable. Verification
must succeed again before an unreadable configuration can be replaced. No URL or
signing key enters audit metadata or ordinary runtime logs.

Inbox availability is independent of callback delivery. A webhook `2xx` means
receiver acceptance, with no claim that the Operator read it. Failed sends do not
remove inbox entries or block provider execution. Deliveries retry transient
failures with backoff for at most eight attempts, preserve the event ID/body and
sign each attempt freshly. `410` stops the subscription; `413` fails that delivery.
Events have no protocol replay cursor; catch up through the retained inbox.

Gmail staleness and encrypted-backup failures also enter the inbox as
`docket.system.alert`, with bounded server-authored text and a public account or
audit reference. Retries keep the first alert for that episode; backup retries do
not flood the inbox. Native-only mode creates no Discord delivery for these
alerts. Existing operational audits and retry behavior remain intact.
Inbox pages also respect a byte budget, so long alert summaries produce a
continuation cursor instead of an oversized tool response or skipped records.

With `DOCKET_DISCORD_ADAPTER_ENABLED=false`, new attention, brief and reminder
projections create inbox records without Discord outbox work. Keep
`DOCKET_PLUGIN_ENABLED=true` for this configuration. This local switch does not
retire a running Hermes gateway or its cron jobs; production cutover requires a
separate drained deployment after native acceptance. Restricted Hermes triage
can remain during the transition, with its separate non-authoritative credential.
The Operator explicitly chose to retire the gateway-managed triage cron as well.
A connected Dot, another scheduled MCP client or a local inference client will
provide triage execution. Docket does not select or host an inference provider.
Ingestion can stage sources while that client is being connected, and committed
reminders/provider work remain active.

The normal deployment script selects checks from this setting. A missing setting
keeps the existing adapter. An explicit native-only deployment retains CI,
execution drain, backup and migration guards, stops Hermes/Discord ingress and
SearXNG, and starts only Docket. Native-only production requires the legacy
interactive credential disabled. Retained Discord controls are inert after
retirement; no projection-listener call or Discord message edit is required.
Later native-only deployments require no gateway, ingress password or Hermes
service credentials.
Postdeploy verifies the private OAuth discovery/anonymous-denial boundary; actual
public ingress and signed-in acceptance still require the connection checks below.
The historical clean-reset workflow is restricted to the legacy adapter; use
forward migrations for this transition.

Run both repository gates, obtain green CI and use the normal authorized backup
and deployment path. Then verify public anonymous denial, resource discovery,
private account linking, the stable profile, actual tool count, read results,
revocation/reconnection and rejection of copied foreground context. Run one source
through the selected agent's triage workflow and verify it creates no canonical or
provider mutations. Verify inbox
and event delivery separately before retiring Discord alerts. The explicit
delegated-write amendment permits canonical writes without a foreground/background
distinction for the approved connection; restricted grants still cannot write.
Verify a new execution cannot commit an unobserved revision.

References: [OpenAI authentication](https://developers.openai.com/plugins/build/auth),
[MCP Events](https://developers.openai.com/plugins/build/mcp-events), and the
[existing Aphelion setup](</srv/workspaces/aphelion/docs/AUTH0_SETUP.md>).

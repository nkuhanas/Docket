# Private Docket plugin setup

This guide is a preparation handoff. The backend, DNS, ingress and plugin
connection have not been deployed or configured by the local implementation.
Keep the current production interface until native linking and notification
acceptance pass. Canonical plugin writes remain disabled pending the foreground
authority contract.

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
   `https://docket.you`, signing algorithm `RS256`, permissions `docket.read` and
   `docket.events` and `docket.triage`, and **Allow Offline Access** enabled.
2. Verify the tenant's **Resource Parameter Compatibility Profile**, as used by
   Aphelion, so a request with `resource=https://docket.you` targets this API.
3. Create a **Native** application named `Docket ChatGPT`, using public PKCE
   token exchange (`none`). Enable Authorization Code and Refresh Token grants,
   refresh-token rotation and the owner's login connection. A new Native
   application's public-client setting may not show a Credentials tab.
4. Give that application explicit **user-delegated** access to the Docket API
   for only the three permissions above. Keep machine-to-machine access absent.
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
authentication (`none`) and scopes `docket.read docket.events docket.triage`; it needs no client
secret. The events permission can be omitted for a connection that only reads.
Auth0 authenticates ChatGPT to Docket. Existing Google OAuth credentials remain
private to Docket's provider adapters and are not passed to the plugin or Auth0.

These provider-account steps need the Operator's signed-in Auth0 dashboard. No
credential extraction or management token is needed in chat.

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
DOCKET_PLUGIN_EVENTS_SCOPE=docket.events
DOCKET_PLUGIN_TRIAGE_SCOPE=docket.triage
```

The ignored `.runtime/plugin/native-oauth.env` contains this candidate overlay
and preserves the current Operator identity explicitly.
It is not loaded automatically and has not changed production `.env`. No OAuth
client secret is needed. The exact callback URI still comes from the eventual
ChatGPT connection, after the public endpoint is available.

Retain the current Operator identity when disconnecting Discord. Set
`DOCKET_OPERATOR_REF` explicitly to that same historical identity; do not invent
a new identity or rewrite prior rows to make account linking work. Enrollment
uses the configured Operator identity and issuer. The local administrative
command previews by default:

```bash
uv run docket-plugin-auth approve --subject '<exact owner subject>' --events --triage
```

Add `--execute` only when the Operator explicitly authorizes production owner
enrollment after migration. The returned `prn_` is the stable public account
identity and can be supplied to `docket-plugin-auth revoke`. Reapproval preserves
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
The actual listener, certificates, firewall, DNS and existing proxy services must
be reviewed by their infrastructure owner before applying this example.

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

Triage may create intelligence, apply existing Preferences and publish cases or
briefs. It cannot stage/commit canonical work, resolve canonical conflicts, create
policy or request Google writes. OAuth `docket.events` only manages subscriptions.
Local clients can also use the private four-tool `/triage-mcp/` service credential,
independently of Hermes. The client manages its own credentials and refresh flow.

## Acceptance before cutover

The native profile exposes 24 tools: the 18 existing reads, the stable account
profile, inbox list/get and three bounded triage tools. Six modern MCP Events
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
execution drain, backup and migration guards, quiesces an existing gateway's
controls, stops Hermes/Discord ingress and SearXNG, and starts only Docket. Later native-only
deployments require no gateway, ingress password or Hermes service credentials.
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
and event delivery separately before retiring Discord alerts. A hosted plugin
connection cannot enable canonical writes until foreground/background separation
or an explicitly approved alternative has been established.

References: [OpenAI authentication](https://developers.openai.com/plugins/build/auth),
[MCP Events](https://developers.openai.com/plugins/build/mcp-events), and the
[existing Aphelion setup](</srv/workspaces/aphelion/docs/AUTH0_SETUP.md>).

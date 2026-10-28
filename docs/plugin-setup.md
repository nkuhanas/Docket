# Private Docket plugin setup

This guide is a preparation handoff. The backend, DNS, ingress, Auth0 and plugin
connection have not been deployed or configured by the local implementation.
Keep the current production interface until native linking and notification
acceptance pass. Canonical plugin writes remain disabled pending the foreground
authority contract.

## Operator action: create the Docket OAuth connection

The existing Aphelion Auth0 tenant and owner can be reused. Create a separate API
and a separate application; Docket must not accept Aphelion's audience or client.

1. Under **Applications → APIs**, create `Docket` with identifier
   `https://docket.you`, signing algorithm `RS256`, permissions `docket.read` and
   `docket.events`, and **Allow Offline Access** enabled.
2. Verify the tenant's **Resource Parameter Compatibility Profile**, as used by
   Aphelion, so a request with `resource=https://docket.you` targets this API.
3. Create a **Native** application named `Docket ChatGPT`, using public PKCE
   token exchange (`none`). Enable Authorization Code and Refresh Token grants,
   refresh-token rotation and the owner's login connection. A new Native
   application's public-client setting may not show a Credentials tab.
4. Give that application explicit **user-delegated** access to the Docket API
   for only the two permissions above. Keep machine-to-machine access absent.
   When RBAC is enabled, assign the owner permissions so the token's `scope`
   claim contains the granted scopes.
5. Retain the new application client ID, issuer URL, JWKS URL and exact owner
   user subject. These are setup identifiers, not access/refresh tokens or a
   client secret. The existing tenant/owner identifiers are already documented
   in Aphelion; only the new Docket client/grants need creating.
6. When the verified server is connected in ChatGPT, register the **exact
   callback URI displayed by that connection** in Auth0. Do not guess it or use
   a wildcard. Complete linking and verify `docket_get_profile` in Dot's own task.

These provider-account steps need the Operator's signed-in Auth0 dashboard. No
credential extraction or management token is needed in chat.

## Prepared runtime configuration

Use the following nonsecret settings only after the corresponding account and
network configuration exists:

```dotenv
DOCKET_PLUGIN_ENABLED=true
DOCKET_PLUGIN_PUBLIC_ORIGIN=https://docket.you
DOCKET_PLUGIN_OAUTH_ISSUER=https://YOUR_AUTH0_TENANT/
DOCKET_PLUGIN_OAUTH_JWKS_URL=https://YOUR_AUTH0_TENANT/.well-known/jwks.json
DOCKET_PLUGIN_OAUTH_CLIENT_IDS=["NEW_DOCKET_NATIVE_CLIENT_ID"]
DOCKET_PLUGIN_READ_SCOPE=docket.read
DOCKET_PLUGIN_EVENTS_SCOPE=docket.events
```

Retain the current Operator identity when disconnecting Discord. Set
`DOCKET_OPERATOR_REF` explicitly to that same historical identity; do not invent
a new identity or rewrite prior rows to make account linking work. Enrollment
uses the configured Operator identity and issuer. The local administrative
command previews by default:

```bash
uv run docket-plugin-auth approve --subject '<exact owner subject>' --events
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

## Acceptance before cutover

Run both repository gates, obtain green CI and use the normal authorized backup
and deployment path. Then verify public anonymous denial, resource discovery,
private account linking, the stable profile, actual tool count, read results,
revocation/reconnection and rejection of copied foreground context. Verify inbox
and event delivery separately before retiring Discord alerts. A hosted plugin
connection cannot enable canonical writes until foreground/background separation
or an explicitly approved alternative has been established.

References: [OpenAI authentication](https://developers.openai.com/plugins/build/auth),
[MCP Events](https://developers.openai.com/plugins/build/mcp-events), and the
[existing Aphelion setup](</srv/workspaces/aphelion/docs/AUTH0_SETUP.md>).

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

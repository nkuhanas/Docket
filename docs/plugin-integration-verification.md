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

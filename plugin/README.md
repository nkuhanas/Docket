# Private Docket plugin source

`docket/` is the self-contained candidate plugin source for the self-hosted Docket
service. It is not an installed plugin or proof that `docket.you` is reachable.
Its endpoint must be verified after the authorized backend/ingress deployment
before exporting or installing a release. No account-side changes have run.

Use the [setup guide](../docs/plugin-setup.md). The package declares the registered
public Auth0 client and read/events/triage plus offline-refresh scopes. A custom
private MCP connection can use the same verified HTTPS endpoint and client.
For hosted ChatGPT, register that connection first and supply its actual URL so
the workflow package can bind its verified App ID. A saved endpoint-only package
does not establish hosted tool availability. Register the exact displayed callback
in Auth0 before linking. Keep credentials out of this directory. This is a
personal plugin; no public submission is planned.

The plugin's generated contract comes from `scripts/generate-tool-contracts.py`.
The source and endpoint expose reads and separately permitted non-authoritative
triage; the connected agent supplies inference and scheduling. Creating or
importing a plugin does not enable canonical writes.

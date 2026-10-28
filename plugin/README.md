# Private Docket plugin source

`docket/` is the self-contained candidate plugin source for the self-hosted Docket
service. It is not an installed plugin or proof that `docket.you` is reachable.
Its endpoint must be verified after the authorized backend/ingress deployment
before exporting or installing a release. No account-side changes have run.

Use the [setup guide](../docs/plugin-setup.md). A custom MCP connection created
as a private plugin is also supported; point it at the verified HTTPS endpoint,
select OAuth and provide the new Docket predefined client ID. Keep credentials
out of this directory. This is a personal plugin; no public submission is planned.

The plugin's generated contract comes from `scripts/generate-tool-contracts.py`.
The source and endpoint expose reads and separately permitted non-authoritative
triage; the connected agent supplies inference and scheduling. Creating or
importing a plugin does not enable canonical writes.

# Local credentials

Place operator-supplied credentials here. Everything except this README is
ignored by Git. Do not copy the dummy Google token from `../smoke/`;
`scripts/setup-google-oauth.sh` generates `google_oauth_token.json` after consent.

Before deploying the October 7 authority amendment, configure a distinct random
`docket_triage_token` (at least 256 bits) and reinstall the isolated triage profile
with `scripts/setup-hermes-triage.sh`. Keep it different from both foreground MCP
and internal service tokens. A missing or shared credential fails authentication;
there is no fallback to foreground authority. Optional read-only clients use
another distinct file configured by `DOCKET_READ_ONLY_TOKEN_FILE`.

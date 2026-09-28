# Event field patching verification

## Sign-off-enablement boundary

`ONT-DELTA-2026-09-28-EVENT-FIELD-PATCHING` is frozen at SHA-256
`fd3c48307f499bf9c1af6aed4e80f202d62965a6c1941bbbee88ad8dd64c6605`.
The private candidate remains under `deltas/`; the package carries only its
sign-off metadata, exact scope, and six prerequisite specification Decisions.
Registration is not ledger sign-off or implementation evidence.

This enablement slice makes no event schema, mutation-service, compiler, MCP
contract, Hermes instruction, provider, or database-migration changes. It does
not repair or replay the UNIV 1000 request. In particular, its last failed draft
attempted cancellation; it must not be committed as a substitute for the
Operator's requested content/location update.

Before implementation, the Operator must send the manifest's exact `signoff_text`
through the authenticated Docket Discord surface. The existing deterministic
recognizer must persist `utt_` → specification-signoff `dec_` → `aud_` → `rsp_`.
Verify that chain and the implementation readiness plan before changing runtime
behavior. Sign-off grants neither production reset nor historical request replay;
production deployment remains separately directed.

Read-only production verification before freeze matched all six exact prerequisite
Decisions, including `dec_01M293W1JA9CZFRMNY3FR2A48D` for September 11. The validator
must check each again at sign-off; a different Decision with the same document and
hash does not satisfy an exact reference prerequisite. No bootstrap is required.

## Verification gates

`test_event_field_patching_signoff.py` checks exact sign-off and durable replay,
each substituted prerequisite, non-exact text and wrong hashes, and zero Event,
EventOccurrence, ChangeSet, or Operation creation. Manifest tests check packaging,
scope, authority flags, and all prior document/hash bindings. Tests use packaged
metadata and synthetic provenance, never ignored candidate files or production
data. Required release gates are the full check and isolated Compose smoke, plus
clean-checkout validation of artifact packaging.

These tests establish sign-off recognition only. Sparse field editing, arbitrary
location text through the new patch path, clear-vs-omit, occurrence preservation,
concurrency, pending-draft migration, and provider delivery still require the
candidate's A01–A13 implementation acceptance evidence after ledger sign-off.

September 28 local enablement verification passed **890 tests, Ruff, and strict
mypy**, including the full gate from a clean Git archive without private source
artifacts or runtime state. Isolated Compose smoke passed authenticated MCP,
restricted ingress selection, synthetic governance export/restore, PostgreSQL
assembly races, and migration downgrade/re-upgrade. These are deterministic
enablement results, not evidence that the field-patching amendment is implemented
or signed.

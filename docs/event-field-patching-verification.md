# Event field patching verification

## Implementation and authority

The exact frozen amendment was signed through authenticated Docket capture:
`utt_01M3NMB407XB49C7JR49ME1CCN` →
`dec_01M3NMB57683N4JWPYXSX8S5Y6` →
`aud_01M3NMB5773MQWHZCFGPNDEYG7` →
`rsp_01M3NMB5CMETB1S2VCDZ4Y0MSR` (delivered).
Read-only verification matched the exact hash, scope, and six prerequisite
Decisions before implementation. The Operator separately directed implementation
and deployment. Neither instruction replays old requests or resets domain data.

The current contract is v43, plugin 0.34.0, ChangeSet compiler 3. No new tool,
database column/table, migration, external lookup, or location registry is needed.
The public field patch is distinct from the internal complete executable record:

```json
{"payload": {"event_spec": {"location": "Async"}}}
```

The rest of the mutation envelope still carries the selected event, exact scope,
basis, and expected version. The ordinary workflow is stage → optional review →
commit. Docket materializes only supplied fields against that canonical version,
not a model-authored snapshot. Null clears notes/location; empty text is retained;
omission preserves the baseline. Title/timing cannot be null. Timing requires
complete bounds and an explicit timezone, with a fold for ambiguous local time.
Routing and recurrence are separate typed capabilities, not content patch fields.

Occurrence edits preserve the original identity and any already moved time,
reuse a replacement child, and leave neighboring dates alone. A content patch
cannot revive a cancelled occurrence. Equal values create no canonical/provider
effect. Explicit series edits preserve and disclose existing occurrence overrides.
Provider creation of a DST-fold replacement carries the retained offset, and
reconciliation distinguishes the two instants. This uses the standard
[Google event start/end contract](https://developers.google.com/workspace/calendar/api/v3/reference/events),
not a new provider capability.

Bound field intent is pinned with the draft inputs. Mechanical retries cannot
change its location, add a different field, widen scope or switch to cancellation.
The binding prevents reinterpretation; it does not independently prove that the
model understood the original utterance. Semantic changes still require the
existing authenticated resolution path. Supporting attachment fields compile
through the same immutable field-evidence proof, including occurrence children.

## Retained work and recovery

The pre-implementation inventory found eleven uncommitted drafts, five with
event modification actions: two early full snapshots, one failed occurrence
cancellation, one one-time full snapshot and one two-occurrence outer-title
recipe. No rows or revisions were rewritten. In particular the incident's
`chg_01M3AKWK0VAJ074NVEFBZB6R4H` remains a failed cancellation draft, not an
approved interpretation of “async.” Do not commit it as a repair.

Old full-snapshot/duplicate-title inputs fail the current public schema. Existing
executable revisions must parse without changing their effect hash; otherwise
commit returns `draft_migration_required`. Restaging an old compiler's event-edit
draft returns `event_patch_migration_required` with authority preserved, instead
of silently applying new compiler meaning. The existing explicit recompile path
requires current typed inputs, effect equality and re-observation; it is not an
old-protocol decoder. If original intended fields cannot be proved, resolve the
specific ambiguity through authenticated context rather than fabricating a patch.
Committed receipts and earlier evidence remain available without reinterpretation.

For a current sparse draft, correct the indicated field/scope error and stage a
new operation under the same request. Replaying a previous operation key returns
its previous result, not a new edit. Version conflicts require reading/reconciling
the changed target; never substitute the latest version blindly. Stage and commit
receipts name changed/cleared fields, no-ops, scope and preserved overrides. A
canonical commit remains committed if Google delivery fails; follow the receipt's
operations rather than creating the event again. Queued is not delivered.

## Implementation acceptance map

| Cases | Deterministic coverage |
| --- | --- |
| A01–A05, A10–A11 | `test_sparse_event_patches.py`: exact UNIV Oct 8/`univ_1000` result, arbitrary text, clear/omit/empty, moved/repeated/no-op, all-day/folds, preserved series overrides. |
| A02–A04, A09, A13 | `test_calendar_scoped_updates.py`, `test_calendar_provider.py`: exact scoped provider field values, untouched other fields, explicit clearing, lost responses, failed preconditions, fold reconciliation. |
| A06–A08, A12 | Sparse schema/repair tests plus existing occurrence/shared-service authority tests: forbidden routing/default snapshot/title, cancelled target, stale version, interpretation changes, retained old-compiler draft. |
| A09 | `compose-assembly-postgres-smoke.py`: independent PostgreSQL connections, two competing sparse edits, only one commit, stage/commit replay, one child/two operations and immutable original identity/preview. Existing assembly race tests cover stale observations and admitted-call ordering. |
| A07, A13 | Actionable field diagnostics and captured stage/commit preview assertions; `test_field_evidence.py` covers sparse text/image changes through stage, repeat stage, commit and exact provenance. |

Release requires the full quality gate, isolated PostgreSQL/Compose smoke, clean
archive contract/package validation and both CI jobs. Record actual results below
after execution. Deterministic provider tests are not a live Google smoke. A live
location-only edit and neighbor check require a separately authorized event target;
the historical UNIV request is not used as a deployment test.

September 28 implementation verification: **955 tests, Ruff and strict mypy
passed**, including the full quality gate in a clean Git archive without private
provenance/runtime files. Final focused checks also reject an empty edit payload
at the schema boundary rather than raising an internal compiler assertion.
Isolated Compose smoke passed authenticated MCP/profile parity, synthetic
governance restore, sparse occurrence PostgreSQL races/replay, immutable revision
and occurrence triggers, provider recovery, and migration downgrade/re-upgrade.
No live calendar event was changed for these tests.

## Historical sign-off-enablement boundary

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

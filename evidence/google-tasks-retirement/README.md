# Google Tasks and NocoDB retirement

[SPEC 1.2](../../SPEC.md) is deployed from `c7d25789ecfb` using the immutable
locally tested image, contract v68 and the same private plugin updated to 2.2.0.
The [implementation matrix](../../docs/google-tasks-implementation.md) distinguishes
implemented behavior, live API evidence and remaining acceptance. Same-host Tasks
journeys and intended-device popup observation remain pending.

[host-context.json](host-context.json) supersedes the initial rescan-pending status:
the Operator confirms fresh ChatGPT Tasks context, and three successful completed
v68 Tasks read calls corroborate it. Host write journeys and device display remain
separate acceptance. The original activation/plugin receipts stay immutable.

## Activation

[activation.json](activation.json) records the tested source/tree/image, 590 passing
snapshot tests, successful native/provider/release/storage groups, PostgreSQL
migration/concurrency proofs, clean synchronized merge, durable drain, backup,
production upgrade to 20261106a7b8, health and native OAuth boundary. It also records
actual runtime method admission for the current account/adapter/grant: insert,
patch, move and delete all pass, with validation fixtures cleaned. Credential
fingerprints, resource bodies and private targets are not published.

[hosted-plugin.json](hosted-plugin.json) records plugin 2.2.0 publication and read-back
with App identity, configuration, presentation and audience preserved. Publication
is separate from ChatGPT's manual MCP rescan and new-tool acceptance. The native
validation Calendar adoption uses a genuine authenticated command, not a manually
inserted production row.

The deployed image advertises 114 tools, including six Tasks workflows. Existing
flat create/update/retract commands own ordinary task lifecycle. Fifteen hidden
aliases are unregistered; historical receipts remain genuine. The eight new or
modified definitions total 12,245 bytes in [contracts.json](contracts.json), below
the per-input, per-definition and combined budgets. Native instructions are 1,889
bytes; the conditional workflow reference avoids injecting a giant catalogue.

## Domain and provider evidence

- [live-provider.json](live-provider.json): actual Google limits, dates, conditional
  writes, completion/reopen, correlation recovery, stable cross-list moves and
  current-subtree behavior; exact private/free popup-zero Calendar rendering and
  UTC transport at the backwards DST boundary. API acceptance is not device display.
- [postgres.json](postgres.json), [reminders.json](reminders.json): earlier isolated
  Task/Item title persistence, genuine policy imports, retained binding revisions,
  competing edits, atomic reminders, replacement and completion cancellation.
- [reminder-inbound.json](reminder-inbound.json): immutable per-generation authority
  and genuine Calendar clock/deletion observation effects through the shared engine;
  copied identities, unsupported changes and revoked grants cannot alter Tasks.
- [subtree-boundaries.json](subtree-boundaries.json): final parent-only lifecycle
  dispatch, independent descendant read-back, concurrent ancestry changes and
  cancellation/reopen. [subtrees.json](subtrees.json) is the earlier slice, superseded
  where it describes individual descendant writes.
- [method-validation-implementation.json](method-validation-implementation.json):
  pre-activation CLI, privacy, target confinement and bounded sibling-order recovery
  proofs. Its pending activation fields are historical, superseded by activation.json.

One-sided/disjoint changes converge; same-field conflicts retain affected work.
Selected-list policies retain genuine request roots and observations without fake
per-poll utterances. Positive tombstones or acknowledged deletes establish deletion;
source absence/404 alone cannot, because a cross-list move also removes membership.
Unknown inserts never blindly repeat. Owned reminders retain typed origins across
cache rebuilds and stay outside workload accounting. AM/PM headers are transport
presentation; canonical Task/Item titles remain clean and lossless.

## Retirement

[inventory.json](inventory.json) records the exact stopped evaluation resources and
protected data inventory. Three authenticated encrypted archives were restored and
inspected in isolated disposable copies. The Operator classified the legacy data
as unnecessary and explicitly authorized disposal without import.

[retirement.json](retirement.json) records removal of six dedicated containers,
four exclusive volumes, two networks and unused NocoDB/evaluation images. No broad
prune or production reset occurred. Protected recovery archives remain separate
from the retired runtime; no fallback, compatibility registry or dual write remains.
The tested image cold-starts and production remains healthy without NocoDB.

## Remaining acceptance

The current method gates are active, not read-only placeholders. Complete the
same-App rescan and actual native Tasks journeys on the designated validation
lists/Calendar. Observe one exact-time popup on the intended device/client and
record the result independently. Do not infer display from Calendar event creation.
Use the [operations procedure](../../docs/google-tasks-operations.md); no new Auth0
permission, callback URL, foreground proof or repeated release approval is needed.

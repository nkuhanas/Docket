# Google Tasks operations

Requirements live in [SPEC.md](../SPEC.md) and its adopted
[1.2 delta](../deltas/2026-11-06T17-59-26Z-google-tasks-and-nocodb-retirement.md).
This procedure does not replace the source-bound local release checks.

## Authorization and readiness

Keep the existing native App, Auth0 client and docket.read/write/triage grants.
Google Tasks uses the backend's separate Google Tasks OAuth scope. There is no
new Auth0 tasks permission, Discord approval or foreground-presence requirement.
docket.events is needed for subscriptions, not Calendar popup delivery.

The Tasks API must be enabled in the Google Cloud project. Extend Google consent
with `scripts/setup-google-oauth.sh --scope-profile tasks --force --credentials-only`
when necessary. Use its fresh documented sign-in link; do not share the sign-in
or callback URL. Existing Calendar/Gmail grants are retained. Ordinary access-token
refresh does not invalidate method evidence; a new grant does.

`DOCKET_TASKS_ENABLED=true` enables discovery/polling through the normal deployed
configuration. Discovered lists start disabled; a current authenticated native
`docket_configure_task_list` command admits each chosen two-way policy. Changing
the default does not publish older local tasks. Disabling preserves canonical
records; detaching preserves both records and their identity exclusion.

## Validate actual provider methods

After schema deployment, `scripts/docket tasks status` reports the current
account/adapter/grant method gates. A token, request header or hand-written
success receipt cannot substitute for actual conditional-write evidence.

Run `scripts/docket tasks validate --targets-file /tmp/docket-task-targets.json`
inside the supported service wrapper. The file must be private (0600) and readable
by the service user. It contains source_list and destination_list IDs for the
operator-authorized “Docket validation” and “Docket validation move target” lists.
It must already exist inside the container; never print credential files or
paste IDs/tokens into general logs. The command verifies the exact lists using
the current Google grant before creating any fixture. The protected local
validation manifest can supply these IDs; no fingerprint editing is necessary.

The probe creates only synthetic records, checks full titles/notes, date
set/preserve/clear, completion/reopening, stale patch/move/delete rejection,
native stable-ID cross-list moves and controlled discarded-response correlation
recovery. It deletes only its own identified fixtures. Existing records are
preserved. Incomplete identity recovery or cleanup remains explicit.

The CLI retains immutable per-method evidence and a safe audit event through
application services. Failed new evidence revokes that method for the tested
grant; do not edit production rows or bypass a failed gate. A grant changing
during the probe records no successful admission. Optional --report writes a
sanitized report; standard output never contains credentials or resource bodies.
Recheck status afterward. This is not a native MCP tool that can self-assert G0.

## Everyday use and recovery

Read docket_get_tasks_context for lists/readiness and docket_query_tasks for work.
Use the existing create/update/retract tools with flat fields. A scheduled day,
deadline and remind_at are separate. An AM/PM header displays the managed time in
Google Tasks; an owned private/free Calendar companion handles the exact alert.
Calendar success is not proof a device displayed a popup.

Use docket_query_tasks with format="json" or format="csv" for full-text export.
Concatenate content chunks and follow next_cursor while present. A captured page is
stable across chunk retries; later pages use current canonical observations.
This adds no second export tool or provider-side write.

Parent completion/deletion/list moves use Google's current subtree, including
new subtasks. One parent lifecycle write affects Google; known descendants have
independent read-back confirmations. Concurrently moved descendants retain actual
mismatch evidence and a coalesced inbox notice rather than receiving individual
writes. Sibling order is a projection hint, not a canonical Task column. Lost
acknowledgements can converge from complete scoped sibling coverage and a fresh
target read without a second move. Changed or incomplete outcomes stay uncertain.

Read the original operation and affected Task before recovery. Unknown inserts
never blindly repeat; use observed candidates and explicit linking when needed.
Source absence cannot establish deletion, because cross-list moves also remove
source membership. Only positive deletion acknowledgements/tombstones confirm it.
Conflicting fields can be resolved independently through docket_resolve_task_sync.

An unsupported all-day reminder edit or revoked reminder grant leaves canonical
work unchanged and creates one useful exception. Reaffirm/correct remind_at through
the Task to repair a diverged owned helper. Task context shows generation,
destination, operation/delivery and binding state. Disabling Task-list syncing
does not mute an independent Calendar reminder.

## Host acceptance

Preserve the existing private App/plugin identity. Publish the prepared skill
update after the backend and method gates work, then rescan the connected server
under MCPs → Issues → Rescan. Verify actual task context, creation/editing,
completion/reopening, hierarchy, conflict/retry and reminder journeys on the
dedicated targets. Separately observe one popup on the intended device/client;
record that outcome honestly rather than infer it from API status.

## Timed delivery recovery

Recovery polling defaults to 15 seconds; individual ambiguous/error backoffs still
apply. An unknown cancellation of an owned retired reminder can retry its original
conditional request only after a fresh read proves the exact original non-wildcard
ETag remains unchanged. Binding refreshes cannot replace a sent cancellation pin.
Changed versions remain visible for reconciliation. This does not allow a retired
creation to run again or a creation to be blindly repeated.

## Device acceptance and diagnosis

The Operator observed both the native Task companion and a plain API control on
the iPhone Google Calendar app at 7:15 PM PST on November 6. Both entries and their
at-time bell settings were confirmed on the phone before that instant. The
companion used the existing private/free one-minute format and zero-minute popup.

Earlier trials missed alerts, including a plain baseline. Late synchronization
to the device is the leading hypothesis, not a proven sole cause or a measured
minimum lead time. For a diagnostic smoke, verify future event visibility before
its instant; this is not a manual setup requirement for ordinary Tasks. Preserve
the requested reminder instant and current privacy policy instead of adding an
invented delivery delay, changing event duration or silently switching channels.

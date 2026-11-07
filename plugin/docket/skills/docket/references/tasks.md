# Tasks and reminders

Start with `docket_get_tasks_context` for lists, readiness and selected task/sync
records. Use `docket_query_tasks` to find work by list, parent, text, state or day;
follow its cursor for remaining results. Context supplies canonical refs and
versions. Six Task workflows extend existing create/update/retract commands;
do not reconstruct provider IDs, change graphs or internal staging payloads.

For an export, use the same query with format="json" or format="csv". Concatenate
each content chunk in order and follow next_cursor while present. Full titles and
notes survive even when one row needs several chunks. Each captured page stays
fixed during continuation; later pages are read when requested. Normal summary
queries remain compact and keep editable context.

For “remind me to file the report Friday at 5 PM,” resolve the date and timezone,
then use `docket_create_task` with title, scheduled_date and remind_at (an ISO
instant including its offset). Optional timezone is an IANA name. Docket derives
the Item, Google representation and private/free Calendar reminder. Interpretation
belongs to the connected agent; Docket validates and persists the concrete values.

Keep the scheduled day, an exact `due` deadline and `remind_at` separate. Google's
public Tasks API stores the day and can clear its opaque native clock. Docket
therefore adds a managed AM/PM header to the Google title while retaining a clean
canonical title. Calendar supplies the exact-time popup. A different reminder day
is labelled explicitly; an over-limit title stays lossless with a display
limitation. Calendar success does not prove a popup appeared on a device.

Use `docket_update_task` for sparse edits. Omitted fields stay unchanged; explicit
null clears a supported field. Completion/cancellation/retraction clears the
reminder. Editing or deleting the exact owned Calendar companion updates or
clears remind_at, independently of list synchronization. All-day or unsupported
edits remain exceptions; unrelated copied events have no authority. Inspect the
Task's reminder delivery/binding status when a popup or edit is missing.

Complete with state="completed" and reopen with state="not_started". Cancellation
removes the Google representation without pretending the work was completed.
Logical removal uses `docket_retract_record`. Parent completion, cancellation,
deletion and list moves affect the current subtree, including Google subtasks
added since the last read. Docket derives descendant effects; no recursive flag
or additional approval is needed. Reopening a parent does not reopen children.
For hierarchy/order use parent_task_ref and after_task_ref; a new child inherits
its parent's list when task_list_ref is omitted. Parent moves preserve Task/Item
and work-history references. Assigned tasks remain observation-only.

Use `docket_configure_task_list` to select observe, two_way or disabled mode,
default list and reminder Calendar. Two-way policy visibly includes Google
completion and deletion propagation. Disabling preserves canonical tasks;
detaching with `docket_link_google_task` preserves both records and stops syncing
that identity. List disappearance never deletes canonical tasks. Refresh queues
discovery/polling with `docket_refresh_tasks`; reads do not pretend a fresh poll.

Sync is field-specific: unrelated edits can converge while one field conflicts.
Read the conflict, then use `docket_resolve_task_sync` for the selected fields and
side. An existing counterpart can be linked explicitly instead of duplicated.
Canonical commit, queued delivery, confirmed delivery and uncertain outcomes are
distinct. Preserve operation_id on unchanged retries and follow the original
delivery refs after timeouts; never create a replacement merely because a
response was lost. A lost delete needs positive deletion evidence, since a
cross-list move also makes the source resource disappear.

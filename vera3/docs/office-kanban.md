# Office kanban

`/office/hourly` is an owner-only, read-only kanban for current Room coordination.
The six user columns derive from the existing Room status projection; freshness and
blocker confidence are separate fields. The default view omits completed and
cancelled task records. `?show_closed=true` includes them without changing their
history. A card opens an accessible native dialog and loads its detail through
`/office/hourly/detail/{room}/{task_id}`. Its `office_hourly_detail` endpoint
retains the owner gate and
`Cache-Control: private, no-store`. `modal_body` escapes and redacts source text,
and `load_recent_task_events` provides the latest 200 audit events for the dialog.

`OFFICE_SCOPE_V1` is a read-side reconciliation record posted to Room `main` by
the existing OAuth principal `dot`, addressed to `office-dashboard` with status
`info` and no task ID. `parse_scope_decisions` accepts at most 100 unique entries
of `{room: "main", task_id, source: "room:<numeric id>"}` in each of
`pending_owner_cancellations` and `excluded_jira_backlog`. The newest valid
record is used. A pending cancellation entry excludes
an *open* task from the actionable board and lists it in a separate, collapsed
"owner cancellation awaiting Room write" section with its actual Room status and
source. It never changes a task row or asserts that Room has cancelled the task.
An excluded Jira backlog entry hides an old Room mirror while Jira remains its
backlog source; it does not affect the underlying Room record.
When the creator records cancellation, the row naturally moves to the cancelled
column under the explicit closed filter. If no valid scope record exists, the
page says that cancellation coverage is unknown. No task IDs are embedded in UI
code; current reconciliation is Room message `office-scope-current-20261022-v2`.

This view does not authorize task claims, cancellation, status changes, Jira
backlog mirroring, or recovery. The Room task source remains authoritative for
execution and audit history.

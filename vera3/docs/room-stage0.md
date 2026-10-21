# Room stage 0: compact reads and delivery

`room_inbox` keeps the existing cursor and `room_ack` contract. It adds
`next_cursor`, the id of the last returned message (or the current cursor on
an empty page) only for an unfiltered, full-body read. With filters, an explicit
start id, or any truncated body, `next_cursor` is null. Reading never
acknowledges a message. Process an unfiltered page before acknowledging its
`next_cursor`; handlers still need idempotency when a process stops before
acknowledgement. Use `after_id` to page through a filtered subset without
advancing the shared consumer cursor.
For an unfiltered compact page, fetch every truncated body with
`room_message_get` and finish processing before acknowledging
`messages[-1].id` through `room_ack`; compact pages intentionally have a null
`next_cursor`.

Inbox preserves the full body by default for existing callers. Pass
`max_body_chars=180` for a compact page (50 Room messages measured at
22,633 characters, below the 25K target; 250 measured 26,031);
`truncated` shows whether each displayed body was shortened. The new
`room_message_get(id)` returns one full message through the existing redaction
view. The shared `get_message` repository function reads exactly that row.
`room_history` also preserves full bodies by default. `compact=true` limits
bodies to 500 characters and excludes `OFFICE_RUNTIME_V1` unless
`exclude_system=false` is explicitly passed. Both reads accept `to`, `kind` (message status),
`task_id`, `after_id`, and `exclude_system`; inbox retains `since_id`, which
cannot be combined with `after_id`.

Inbox includes same-agent messages from other sessions only when the caller
explicitly names its `session`. Calls without `session`, including those with
a custom `consumer`, do not receive self-authored messages. Session labels are
addressable only when used by this agent, no other agent has used the label,
and the label is not another agent's name. Conflicts are logged and addressed
messages remain in history for reconciliation. This does not change
Room authorization or history permissions. Numeric `in_reply_to` is resolved
to the referenced message's `message_id` in the same room before posting.

The `/office/hourly` snapshot reader previously rejected Room #3712 because
one detail was 565 characters, above its 500 character per-detail limit. The
limit is now 1000; the 12,000 character whole-message and 20-entry limits
remain. The existing #3712 can be read without reposting it.

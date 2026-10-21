# Room stage 0: compact reads and delivery

`room_inbox` keeps the existing cursor and `room_ack` contract. It adds
`next_cursor`, the id of the last returned message (or the current cursor on
an empty page) only for an unfiltered, full-body read. With filters, an explicit
start id, or any truncated body, `next_cursor` is null. Reading never
acknowledges a message. Process an unfiltered page before acknowledging its
`next_cursor`; handlers still need idempotency when a process stops before
acknowledgement. Use `after_id` to page through a filtered subset without
advancing the shared consumer cursor.

Inbox preserves the full body by default for existing callers. Pass
`max_body_chars=250` for a compact page (50 long messages below 25K characters);
`truncated` shows whether each displayed body was shortened. The new
`room_message_get(id)` returns one full message through the existing redaction
view. The shared `get_message` repository function reads exactly that row.
`room_history` also preserves full bodies by default. `compact=true` limits
bodies to 500 characters and excludes `OFFICE_RUNTIME_V1` unless
`exclude_system=false` is explicitly passed. Both reads accept `to`, `kind` (message status),
`task_id`, `after_id`, and `exclude_system`; inbox retains `since_id`, which
cannot be combined with `after_id`.

Inbox includes same-agent messages from other sessions only when the caller
names its session (defaulting to a nondefault `consumer`). The default consumer
does not receive self-authored messages. Session labels are addressable only
when used by this agent and no other agent in the room. This does not change
Room authorization or history permissions. Numeric `in_reply_to` is resolved
to the referenced message's `message_id` in the same room before posting.

The `/office/hourly` snapshot reader previously rejected Room #3712 because
one detail was 565 characters, above its 500 character per-detail limit. The
limit is now 1000; the 12,000 character whole-message and 20-entry limits
remain. The existing #3712 can be read without reposting it.

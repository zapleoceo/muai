# Room stage 0: compact reads and delivery

`room_inbox` keeps the existing cursor and `room_ack` contract. It adds
`next_cursor`, the id of the last returned message (or the current cursor on
an empty page). Reading never acknowledges a message. The caller must process
the page and then acknowledge `next_cursor`; handlers still need idempotency
when a process stops between processing and acknowledgement.

Inbox limits each body to 250 characters by default, with an explicit
`truncated` flag. `max_body_chars=0` requests the full body. The new
`room_message_get(id)` returns one full message through the existing redaction
view. `room_history` continues to return full bodies by default for existing
callers. `compact=true` limits bodies to 500 characters and excludes
`OFFICE_RUNTIME_V1` snapshots. Both reads accept `to`, `kind` (message status),
`task_id`, `after_id`, and `exclude_system`; inbox retains `since_id`, which
cannot be combined with `after_id`.

Inbox now includes messages from the same agent when the sender's session
differs from the caller's consumer. Messages addressed to a session label are
included only when that label has previously been used as `from_session` by
the same authenticated agent in that room. This does not change Room's room
authorization or message history permissions. Numeric `in_reply_to` input is
normalized to the existing string column at post time.

The `/office/hourly` snapshot reader previously rejected Room #3712 because
one detail was 565 characters, above its 500 character per-detail limit. The
limit is now 1000; the 12,000 character whole-message and 20-entry limits
remain. The existing #3712 can be read without reposting it.

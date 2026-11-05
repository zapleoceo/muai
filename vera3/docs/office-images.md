# Office image handoff

## Private files and bounded storage

New uploads also accept UTF-8 TXT/CSV/JSON documents up to 64 KiB. JSON must
parse, binary control bytes are rejected, and archives, executables, HTML and
PDF are not supported. Files are untrusted data, never instructions or code to
execute. `verify_document` checks the document content. The existing byte PUT
retains its `/images/{index}` path for compatibility; the authenticated service
principal and digest checks are unchanged. Unsupported documents keep their
legacy metadata-only representation and are not claimed as transferred bytes.

After claiming a task, an executor can use the existing `room_message_get(id)`
on its Room request. `message_files` adds `attachments` only for the actual
OAuth actor holding a live lease. Documents contain exact UTF-8 content;
images contain base64 bytes. Every attachment includes its size and SHA-256.
This works with clients that have not refreshed their new tool inventory.
`read_attachment` checks the lease, expiry and stored digest; `room_task_image`
remains available for clients that expose it.

New bytes expire 24 hours after creation of the intake, not after each retry.
Reads and reuploads reject expired content. `require_capacity` serializes
PostgreSQL writers with `lock_storage` and caps all new temporary bytes at
64 MiB; a full store returns retryable HTTP 503 (`StorageFull`). The existing
MCP maintenance loop calls `prune_expired` at startup and every six hours, so
expired bytes leave the live table within six additional hours while the loop
is enabled. A stopped loop fails closed at the capacity limit. Task metadata
and digests remain; the file cannot be resurrected by retrying its old intake.
Existing rows with no expiry are preserved; migration 062 does not backfill
or delete their contents. Database backups follow the existing backup policy,
so live-table expiry does not imply erasure from historical backups.

Apply `062_office_file_expiry.sql` before running this version's maintenance
loop. No public URL is created. Public artifact links are suitable only for
files explicitly approved for public disclosure: possession and forwarding
can expose their contents, and caches may outlive a link expiry. Confidential
files stay behind the existing OAuth lease boundary.

The existing Veranda Telegram outbox sends the task and image to Office. It uses
the existing Telegram bot credential to fetch bytes and the existing Office
service principal to upload them. Neither credential is sent to a Room agent.

## Protocol

1. For each photo or image document, Veranda downloads the Telegram file,
   checks its MIME, dimensions and size, then includes `mime`, `size`, and
   lowercase `sha256` in the Office task attachment. The stable
   `client_task_id` is reused after a lost response.
2. `POST /office/v1/tasks` persists an image task as `pending_media`. It does
   not open the Room task or notify an executor yet. This prevents a text-only
   pickup when the image upload has failed.
3. Veranda sends raw bytes to `PUT /office/v1/tasks/{task_id}/images/{index}`
   using its Office bearer credential. Office validates the declared size and
   hash, decodes the image, permits JPEG, PNG and WebP, limits each file to
   4 MiB and 16 million pixels, and checks the creator name and project.
   A task can hold at most four images and 8 MiB of image bytes in total.
   Concurrent uploads of one task serialize on its origin row. A replay with
   the same bytes succeeds; a different payload or principal is rejected.
4. When every declared image has arrived, Office opens the existing Room task
   once and posts the request with the image indexes. The executor claims it
   with the existing `room_task_claim` fencing lease, then invokes
   `room_task_image(task_id, index)` via Room OAuth. The MCP response contains
   image content and its SHA-256. Only the actor holding a live lease can read
   it. An Office service token cannot invoke Room tools.
5. `room_task_claim` is the executor receipt. Veranda's existing Office poller
   observes `lease_live` and changes the local task from `waiting_executor` to
   `in_progress`. Room `ready` events can wake already subscribed agents;
   an agent that is not subscribed must still check its queue. Room inbox
   delivery alone is not an executor receipt.

The Veranda sender marks its outbox row sent only after the image upload
returns `ready=true` with the expected hash. A transport failure leaves the
row pending for retry. An invalid image is a visible failed request and must
be corrected at the source; the sender does not silently dispatch text alone.

## Code map

`OfficeImageRow` stores the verified content and digest under the Office task
origin. `expected_images` selects image attachment indexes. `verify_image`
checks bytes against the declared metadata and decodes the image. `put_image`
serializes and stores an upload; `all_images_stored` checks readiness, and
`complete_image_task` opens the Room task when ready. `read_image` enforces
the live lease and rechecks content integrity for `room_task_image`.

## Release verification

Apply migration `061_office_task_images.sql` and deploy Office with Pillow
before enabling the Veranda sender. Verify a real image through Telegram ->
Veranda outbox -> Office upload -> Room task -> agent lease -> `room_task_image`.
Compare hashes at upload and read. Record the actual executor claim and
response. A successful HTTP upload alone does not prove pickup. Verify
credential isolation and failed upload behavior. Roll out the Veranda sender
after the Office MCP endpoint is live, then monitor pending/failed outbox rows.
The checked Veranda branch still has `cron/office_poll.php` commented out in
`cron/crontab.txt`; the comment calls for an owner decision and existing
`OFFICE_API_URL` / `OFFICE_SERVICE_TOKEN` configuration. Verify the actual
production schedule and credentials through the approved deployment process
before claiming an operational pickup time. Do not create a second poller.

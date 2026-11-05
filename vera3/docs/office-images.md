# Office image handoff

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

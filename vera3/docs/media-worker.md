# media-worker

Recognizes photo (vision) and voice/audio (whisper) for events with
`triage_status='media_pending'`.

Code layout (one responsibility per file):
- `recognize.py` — download, vision + whisper via broker, entity-cache
  warm-up (`warm_entity_cache`)
- `repository.py` — claim/lease, success/failure bookkeeping, retry policy
- `__main__.py` — the poll loop gluing the two

## Flow

1. ingestor-telegram saves a photo/voice/audio message with placeholder text
   (e.g. `[voice: 12s]`) and `triage_status='media_pending'` plus
   `metadata.media_kind`, `chat_id`, `msg_id`.
2. On startup the worker calls ingestor-telegram `POST /tools/list_dialogs`
   (`warm_entity_cache`, up to 30 tries × 10s): a fresh Telethon session
   resolves rare peers only after seeing them once — without the warm-up,
   `/media/download` on such peers fails with "Could not find the input
   entity". Best-effort: the worker starts either way.
3. media-worker polls pending events (batch of 3, every 10s), **voice/audio
   first, then newest** (`ORDER BY (media_kind IN ('voice','audio')) DESC,
   id DESC`). Speech goes through the fast, cheap whisper pool and is the
   most valuable content, so it is never stuck behind slow free-tier vision;
   within each class, live media (highest id) is claimed ahead of re-processed
   backlog (lower id), so a bulk requeue of old recognition failures can't
   starve fresh incoming messages — see "Re-recognising the backlog" below.
   While the **vision** circuit is open (budget cap / no provider), the loop
   claims `voice_only=True` — photos wait for vision to recover, but voice/audio
   keep transcribing through the separate whisper pool (which isn't capped), so
   a vision cap no longer stalls speech. `_claim_batch`
   does the claim as ONE atomic `UPDATE ... FOR UPDATE SKIP LOCKED ...
   RETURNING` that stamps a 10-minute `media_next_retry_at` lease on the
   selected rows — a plain `SELECT ... FOR UPDATE` in its own transaction
   released the lock the moment the `SELECT` closed, before the row was
   actually processed, so a second worker instance (or the next poll
   tick, if finalize crashed) could re-claim and double-process the same
   event.
4. For each: POST to ingestor-telegram `/media/download` → bytes + mime.
   That endpoint downloads through `ingestor_telegram.media_download.
   download_capped()`, which enforces the 25 MB cap **before** any bytes
   move (via `msg.file.size` from message metadata, raising `MediaTooLarge`)
   and bounds the transfer with a 55 s timeout (`MediaDownloadTimeout`).
   Before this (2026-07-19) the file was buffered whole into RAM and the
   size checked afterward — a large Telegram audio (podcasts run to
   hundreds of MB, the format allows up to 2 GB) OOM-killed the ingestor,
   and because media-worker gives up after 60 s the server-side download
   was left orphaned, buffering into memory nothing was reading. Oversize
   / timeout now come back as errors classified **permanent** by
   `_is_permanent`, so the event degrades immediately instead of burning
   three pointless retries.
5. Photo → **broker `POST /v1/chat?capability=vision`** with OpenAI-style
   multimodal content (`text` block + `image_url` data-URI) and an
   OCR/caption prompt (Russian, 1-3 sentences + verbatim text under
   `Текст:` if readable). The broker picks a vision key (gemini →
   anthropic → openai) — no provider keys live in media-worker.
6. Voice/audio → **broker `POST /v1/transcribe`** (multipart upload).
   Whisper is hosted broker-side since 2026-07-18 (the local `asr-local`
   experiment was removed the same day — the backlog of ~3.3k failed
   voices had been cleared by a one-off local faster-whisper run,
   `media_recognition=ok_local`). Empty transcription (silence) becomes
   `(тишина/неразборчиво)`.
7. On success: append `\n--- recognized photo ---\n<text>` (or
   `voice transcription` / `audio transcription`) to `content_text`,
   set `triage_status='pending'` so normal triage takes over. For
   voice/audio the source is recorded in
   `metadata.media_recognition='ok_broker'`. Finalize SQL guards on
   `triage_status='media_pending'` so a worker that outlived its lease
   can't append the text a second time.

## Retry + degrade (no more queue stalls)

Recognition failures used to keep the event in `media_pending`; because
the claim is `ORDER BY id`, the same first-N events looped forever while
the rest of the queue (87k events at peak) never advanced.

Now each failure is tracked in metadata:
- `metadata.media_retry_count` — incremented per attempt
- `metadata.media_next_retry_at` — claim skips events whose window hasn't
  elapsed (backoff 2m, 15m, 60m), so the queue advances past failures

Outcomes:
| Kind | Result |
|---|---|
| Success | append recognized text, `triage_status='pending'` |
| Transient fail (broker 5xx/429, network) | `media_pending` + backoff, up to 3 tries |
| Permanent (4xx scope/bad-req, 413 oversize, empty text) | **degrade now** |
| After 3 transient tries | **degrade** |

**Degrade** = keep the placeholder (`[photo]`/`[voice: Ns]`), set
`triage_status='pending'` + `metadata.media_recognition='failed'`. The
event still enters the brain — recognition is best-effort, media is never
lost. When keys are added later, re-seed degraded events if desired.

## Env

- `INTERNAL_SECRET` — required, used to call ingestor-telegram
- `TELEGRAM_TOOLS_URL` — default `http://ingestor-telegram:8000`
- `BROKER_URL` — broker base, e.g. `https://aib.zapleo.com`
- `BROKER_PROJECT_KEY` — `aib_prj_…`; project must hold `llm:vision` +
  `llm:audio` scopes (set on the `vera` project)
- `MEDIA_POLL_S` (default 10), `MEDIA_BATCH` (default 3)

No provider keys here — vision/whisper keys live in the broker. Whisper
audio is capped at 25 MB (mirrors the broker's limit); larger files degrade.

## Cost

Goes through the broker's free-first chains:
- Vision: gemini free → anthropic → openai
- Whisper: broker-hosted whisper → provider fallback (см. конфиг брокера)

## Media kinds

| Kind | Recognized? | How |
|---|---|---|
| photo / image (private / group) | ✅ | vision |
| photo / image (broadcast channel) | ❌ | placeholder `[photo]` kept, no vision |
| sticker | ❌ | placeholder `[sticker: <emoji>]` only |
| voice / audio | ✅ | broker whisper |
| video / video_note | ❌ | not processed |
| document | ❌ | not processed |

The recognition gate is one pure policy — `vera_shared.media_policy.
should_recognize_media(media_kind, chat_kind)` — so every ingest path decides
identically and it's unit-tested. Set 2026-07-20 (Dima): the free vision pool
kept getting exhausted by news-channel graphics and stickers — content with
~zero value for searching Dima's own memory. So channel images and all
stickers now skip vision entirely; the event still lands in the brain with its
`[photo]`/`[sticker]` placeholder, only the recognised text is dropped.
Voice/audio go through the separate (uncapped) whisper pool and are recognised
everywhere, including channels.

Earlier (2026-06-29 → 2026-07-20) static `image/webp` stickers were sent to
vision and channel photos were recognised; both were rolled back here.

## Re-recognising the backlog

When recognition fails, the event **still lands in the brain** — `_on_failure`
degrades it to `triage_status='pending'` keeping the placeholder
(`[photo]`/`[voice: Ns]`) + metadata, and normal triage embeds/entity-extracts
it. Only the *recognised text* is missing; the message is never lost.

History left a large pile of such degradations (audited 2026-07-19): ~87.7k
events with `metadata.media_recognition='failed'`, ~79.5k of them recoverable
— they failed on transient capacity errors (`vision 503 no provider`,
`whisper 503`, `GEMINI_API_KEY not set`, transient 502s), which are now fixed
by the LLM circuit breaker + broker-hosted whisper. The unrecoverable tail
(~8.1k) is `Could not find the input entity` (peer not in session cache),
`message not found` (deleted), and `too large`.

To re-recognise, reset recoverable failures back to `media_pending` and let
the hardened pipeline reprocess them:

```sql
UPDATE events e SET
  triage_status='media_pending', triage_error=NULL,
  metadata = (e.metadata - 'media_recognition' - 'media_retry_count' - 'media_next_retry_at')
WHERE e.metadata->>'media_recognition'='failed' AND e.triage_status='done'
  AND COALESCE(e.triage_error,'') NOT LIKE '%Could not find the input entity%'
  AND COALESCE(e.triage_error,'') NOT LIKE '%message not found%'
  AND COALESCE(e.triage_error,'') NOT LIKE '%too large%'
  AND COALESCE(e.triage_error,'') NOT LIKE '%413%';
```

The bottleneck is **vision-pool capacity** — free-tier vision clears only
~130 photos/day, so the 72k photo tail drains over months. This is safe and
non-disruptive because of the claim order (step 3): voice/audio drain first
through the fast whisper pool (~5.3k clear in days), live media always jumps
the queue within its class, the photo backlog chips away on spare vision
capacity, the circuit breaker paces it under the daily budget cap, and every
download is size-capped (no OOM). A host cron `vera-media-requeue.sh` (every
3 h) tops the queue up to ~800 from the recoverable backlog so it drains
steadily without a giant permanent `media_pending`.

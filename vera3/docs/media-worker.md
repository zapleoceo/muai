# media-worker

Recognizes photo (vision) and voice/audio (ASR) for events with
`triage_status='media_pending'`.

Code layout (one responsibility per file):
- `recognize.py` — download, vision via broker, audio via asr-local →
  broker fallback, entity-cache warm-up (`warm_entity_cache`)
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
3. media-worker polls pending events (batch of 3, every 10s). `_claim_batch`
   does the claim as ONE atomic `UPDATE ... FOR UPDATE SKIP LOCKED ...
   RETURNING` that stamps a 10-minute `media_next_retry_at` lease on the
   selected rows — a plain `SELECT ... FOR UPDATE` in its own transaction
   released the lock the moment the `SELECT` closed, before the row was
   actually processed, so a second worker instance (or the next poll
   tick, if finalize crashed) could re-claim and double-process the same
   event.
4. For each: POST to ingestor-telegram `/media/download` → bytes + mime.
5. Photo → **broker `POST /v1/chat?capability=vision`** with OpenAI-style
   multimodal content (`text` block + `image_url` data-URI) and an
   OCR/caption prompt (Russian, 1-3 sentences + verbatim text under
   `Текст:` if readable). The broker picks a vision key (gemini →
   anthropic → openai) — no provider keys live in media-worker.
6. Voice/audio → **LOCAL-FIRST**: `POST /transcribe` on
   [asr-local](./asr-local.md) (faster-whisper small int8, free, no keys).
   Only if asr-local is unreachable / errors does the worker fall back to
   **broker `POST /v1/transcribe`**. The broker's chronic 502/503
   ("no transcription key available") is exactly why local goes first.
   Empty transcription (silence) becomes `(тишина/неразборчиво)`.
7. On success: append `\n--- recognized photo ---\n<text>` (or
   `voice transcription` / `audio transcription`) to `content_text`,
   set `triage_status='pending'` so normal triage takes over. For
   voice/audio the source is recorded in
   `metadata.media_recognition` = `ok_local` | `ok_broker`.

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

An asr-local failure alone never degrades an event — the broker fallback
runs first, and only the final error enters the retry/degrade policy.

**Degrade** = keep the placeholder (`[photo]`/`[voice: Ns]`), set
`triage_status='pending'` + `metadata.media_recognition='failed'`. The
event still enters the brain — recognition is best-effort, media is never
lost. When keys are added later, re-seed degraded events if desired.

## Env

- `INTERNAL_SECRET` — required, used to call ingestor-telegram
- `TELEGRAM_TOOLS_URL` — default `http://ingestor-telegram:8000`
- `ASR_LOCAL_URL` — default `http://asr-local:8000`; set empty to
  disable the local path (broker-only)
- `ASR_LOCAL_TIMEOUT_S` — default 1800; local whisper on 1 CPU thread is
  slower than real-time on long audio, we wait rather than cut
- `BROKER_URL` — broker base, e.g. `https://aib.zapleo.com`
- `BROKER_PROJECT_KEY` — `aib_prj_…`; project must hold `llm:vision` +
  `llm:audio` scopes (set on the `vera` project)
- `MEDIA_POLL_S` (default 10), `MEDIA_BATCH` (default 3)

No provider keys here — vision/whisper keys live in the broker. Audio is
capped at 25 MB (mirrors the broker's and asr-local's limit); larger
files degrade.

## Cost

- Voice/audio: **$0** in the normal case (asr-local). Broker fallback:
  groq whisper-large-v3-turbo free → openai whisper-1.
- Vision: broker free-first chain gemini free → anthropic → openai.

## Media kinds

| Kind | Recognized? | How |
|---|---|---|
| photo | ✅ | vision |
| sticker (static `image/webp`) | ✅ | vision (`recognized sticker`) |
| sticker (animated `.tgs` / video `.webm`) | ⬇ placeholder | emoji alt-text only — not an image |
| voice / audio | ✅ | asr-local → broker whisper |
| video / video_note | ❌ | not processed |
| document | ❌ | not processed |

Stickers were enabled 2026-06-29 (user wanted all images + stickers).
The ingestor sets `needs_recognition=True` only for `image/webp` stickers;
animated/video stickers keep their `[sticker: <emoji>]` placeholder since
they aren't single images.

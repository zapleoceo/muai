# media-worker

Recognizes photo (vision) and voice/audio (whisper) for events with
`triage_status='media_pending'`.

## Flow

1. ingestor-telegram saves a photo/voice/audio message with placeholder text
   (e.g. `[voice: 12s]`) and `triage_status='media_pending'` plus
   `metadata.media_kind`, `chat_id`, `msg_id`.
2. media-worker polls these events (batch of 3, every 10s) using
   `FOR UPDATE SKIP LOCKED`.
3. For each: POST to ingestor-telegram `/media/download` → bytes + mime.
4. Photo → **broker `POST /v1/chat?capability=vision`** with OpenAI-style
   multimodal content (`text` block + `image_url` data-URI) and an
   OCR/caption prompt (Russian, 1-3 sentences + verbatim text under
   `Текст:` if readable). The broker picks a vision key (gemini →
   anthropic → openai) — no provider keys live in media-worker.
5. Voice/audio → **broker `POST /v1/transcribe`** (multipart upload).
   Broker routes groq whisper-large-v3-turbo (free) → openai whisper-1.
6. On success: append `\n--- recognized photo ---\n<text>` (or
   `voice transcription` / `audio transcription`) to `content_text`,
   set `triage_status='pending'` so normal triage takes over.

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
- Whisper: groq whisper-large-v3-turbo free → openai whisper-1

## Media kinds

| Kind | Recognized? | How |
|---|---|---|
| photo | ✅ | vision |
| sticker (static `image/webp`) | ✅ | vision (`recognized sticker`) |
| sticker (animated `.tgs` / video `.webm`) | ⬇ placeholder | emoji alt-text only — not an image |
| voice / audio | ✅ | whisper |
| video / video_note | ❌ | not processed |
| document | ❌ | not processed |

Stickers were enabled 2026-06-29 (user wanted all images + stickers).
The ingestor sets `needs_recognition=True` only for `image/webp` stickers;
animated/video stickers keep their `[sticker: <emoji>]` placeholder since
they aren't single images.

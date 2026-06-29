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
4. Photo → **Gemini `generateContent` DIRECT** (GEMINI_API_KEY) with
   OCR/caption prompt (Russian, 1-3 sentences + verbatim text under
   `Текст:` if readable). The broker (aib.zapleo.com) is text-only and
   422s on multimodal content, so vision bypasses it — same as whisper.
5. Voice/audio → Groq Whisper `whisper-large-v3-turbo` DIRECT call.
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
| Transient fail (5xx, 429, network) | `media_pending` + backoff, up to 3 tries |
| Permanent (key missing, 4xx, safety-block) | **degrade now** |
| After 3 transient tries | **degrade** |

**Degrade** = keep the placeholder (`[photo]`/`[voice: Ns]`), set
`triage_status='pending'` + `metadata.media_recognition='failed'`. The
event still enters the brain — recognition is best-effort, media is never
lost. When keys are added later, re-seed degraded events if desired.

## Env

- `INTERNAL_SECRET` — required, used to call ingestor-telegram
- `TELEGRAM_TOOLS_URL` — default `http://ingestor-telegram:8000`
- `GROQ_API_KEY` — required for voice/audio (else they degrade)
- `GEMINI_API_KEY` — required for photo (else they degrade)
- `GEMINI_VISION_MODEL` — default `gemini-2.0-flash`
- `MEDIA_POLL_S` (default 10), `MEDIA_BATCH` (default 3)

## Cost

- Vision: Gemini 2.0 Flash free tier (AI Studio) ~15 RPM, then ~$0.0001/img
- Whisper: `whisper-large-v3-turbo` Groq free tier ~25 RPM, $0.04/hr beyond
- Video, video_note, sticker — NOT recognized (user decision).

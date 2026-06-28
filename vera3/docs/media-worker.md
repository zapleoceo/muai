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
4. Photo → broker `chat(capability='vision')` with OCR/caption prompt
   (Russian, 1-3 sentences + verbatim text under `Текст:` if readable).
5. Voice/audio → Groq Whisper `whisper-large-v3-turbo` direct call (broker
   doesn't passthrough audio multipart yet).
6. On success: append `\n--- recognized photo ---\n<text>` (or
   `voice transcription` / `audio transcription`) to `content_text`,
   set `triage_status='pending'` so normal triage takes over.

## Failure modes

| Kind | Status | Reason |
|---|---|---|
| Download fail (deleted, no access) | `error` | hard fail, no retry |
| Size >25 MB | `error` | Whisper hard limit |
| Broker/Whisper transient | `media_pending` | retry on next loop |
| Unknown media_kind | `error` | shouldn't happen — fix ingestor |

## Env

- `INTERNAL_SECRET` — required, used to call ingestor-telegram
- `TELEGRAM_TOOLS_URL` — default `http://ingestor-telegram:8000`
- `GROQ_API_KEY` — required for voice/audio
- `BROKER_URL` / `BROKER_PROJECT_KEY` — required for vision
- `MEDIA_POLL_S` (default 10), `MEDIA_BATCH` (default 3)

## Cost

- Vision: Gemini 2.0 Flash ~$0.0001/image at current broker pricing
- Whisper: `whisper-large-v3-turbo` Groq free tier ~25 RPM, $0.04/hr beyond
- Video, video_note, sticker — NOT recognized (user decision).

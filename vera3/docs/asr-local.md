# asr-local

Local speech-to-text service: faster-whisper (`small`, int8, CPU) in its
own container on the compose network. Exists because voice transcription
through the broker chronically failed with 502/503 ("no transcription key
available") — 3.5k voices piled up unrecognized. Local ASR is free,
private, and always available; the broker stays as fallback only.

## API

- `GET /healthz` — `{ok, service, model, model_loaded}`.
- `POST /transcribe` — body is either **raw audio bytes** (any
  `Content-Type` except JSON) or **JSON** `{"b64": "<base64 audio>"}`.
  Query: `language` (default `WHISPER_LANGUAGE`=`ru`; `auto` = let
  whisper detect). Max 25 MB (mirrors the Whisper/broker limit).
  Returns `{"text", "duration_s", "language"}`. Empty `text` = silence.

No auth: the port is `expose`-only (compose network, not published to the
host), same trust model as ingestor-telegram's internal port.

## Model lifecycle

`get_model()` is the module singleton: the model loads once (at startup
via lifespan preload, ~30-60s) and lives for the process. It is baked
into the Docker image at build time — startup needs no network. Requests
are serialized with an `asyncio.Lock` and decoded in a worker thread:
the host has 2 cores shared with production Stepan2, so parallel decodes
would only thrash (hence also `cpus: 1.0`, `mem_limit: 1500m`,
`WHISPER_CPU_THREADS=1` in compose).

Speed on 1 CPU thread is slower than real-time for long audio — callers
must use generous timeouts (media-worker uses `ASR_LOCAL_TIMEOUT_S`,
default 1800s). We wait, we don't cut.

## Env

- `WHISPER_MODEL` (default `small`) — changing it requires an image
  rebuild to re-bake the model.
- `WHISPER_CPU_THREADS` (default `1`)
- `WHISPER_LANGUAGE` (default `ru`)

## Consumers

media-worker voice/audio recognition (local-first, broker fallback) —
see [media-worker.md](./media-worker.md). The one-off backlog re-run of
old failed voices used the same model in a temporary `voice-asr`
container; the permanent service replaces it.

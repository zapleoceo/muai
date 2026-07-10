# Architecture

## Services

```
              ┌───────────── ingestors ─────────────┐
              │  ingestor-telegram (userbot+tools)  │
              │  ingestor-gmail    (OAuth polling)  │
              │  ingestor-instagram (instagrapi)    │
              └────────────────┬────────────────────┘
                               │ POST /event/<source>
                               ▼
                       ┌───────────────┐
                       │   gateway     │  events table (Postgres + pgvector)
                       └───┬───────────┘
                           │
                  triage queue (FOR UPDATE SKIP LOCKED)
                           │
                           ▼
                  ┌─────────────────┐    ┌─── brain-search ────┐
                  │  brain-triage   │    │  ReAct agent loop   │
                  │  (LLM classify) │    │  + embedding search │
                  └────────┬────────┘    └──────────┬──────────┘
                           │                       │
                           └──────► AIbroker ◄─────┘
                                    │
                                    └─► free + paid LLM providers
                                        (cerebras/groq/gemini/anthropic/…)
                           │
                           ▼
              ┌─────────── bot-telegram ───────────┐
              │  @Dimondra_Ai_Bot — owner-only DM  │
              └────────────────────────────────────┘
              ┌─────────── dashboard ──────────────┐
              │  /events /sources /settings /search  │
              └────────────────────────────────────┘
```

## Containers (vera3)

| Container | Purpose |
|---|---|
| `vera3-postgres` | All state. pgvector for embeddings. |
| `vera3-gateway` | `POST /event/<source>` — single ingest endpoint with X-Internal-Secret |
| `vera3-brain-triage-N` | Scalable workers (`docker compose up -d --scale brain-triage=3`). SELECT FOR UPDATE SKIP LOCKED → atomic claim. |
| `vera3-brain-search` | FastAPI `/search` — ReAct agent loop, calls AIbroker. |
| `vera3-bot-telegram` | aiogram polling — DM to owner |
| `vera3-ingestor-telegram` | Telethon userbot + FastAPI tools server on :8000 |
| `vera3-ingestor-gmail` | OAuth refresh + Gmail API polling |
| `vera3-ingestor-instagram` | instagrapi inbox polling |
| `vera3-dashboard` | HTMX UI on :8003 |
| `vera3-prune` | docker system prune --filter='until=72h' daily |

## Event lifecycle

1. Source pushes an envelope to `gateway /event/<source>` with internal secret.
2. Gateway dedupes by `source_event_id`, inserts row in `events` with
   `triage_status='pending'`.
3. `brain-triage` claims a batch (`UPDATE … FOR UPDATE SKIP LOCKED RETURNING`).
4. For each event: build prompt → `chat_async()` submit+poll AIbroker
   `chat:fast` (`/v1/jobs`, see `llm-broker.md`) → parse JSON metadata
   (importance, topics, people, signals) → update row to `done`.
5. Embedding worker (same loop): one Voyage call per batch → row in the
   separate `event_embeddings` table (migration 011 — `events` itself has
   no embedding column anymore).
6. `brain-search` queries events via FTS + cosine on demand.

## Triage queue scaling

`UPDATE … FOR UPDATE SKIP LOCKED` makes N workers race-safe. Default is
`BRAIN_TRIAGE_REPLICAS=5` × `TRIAGE_CONCURRENCY=10` (see `brain.md` for the
current tuning history). Override per-deploy via `.env` or
`docker compose up -d --scale brain-triage=N`.

## DB connection pool sizing

`vera_shared/db/engine._pool_kwargs()` — every service's Postgres pool is
`pool_size=3, max_overflow=7` by default (env: `DB_POOL_SIZE`,
`DB_MAX_OVERFLOW`), down from a flat `pool_size=10, max_overflow=20`.
~13 services × 10 idle connections was ~57 idle Postgres connections
sitting in swap on the host; a service that genuinely needs more can
override via env without touching code. Split into its own function so
it's unit-testable (`tests/unit/test_db_engine.py`) without needing a
real Postgres connection — the branch that calls it is skipped entirely
for SQLite, which is what the test suite runs on.

## Self-healing

- `vera3-monitor.sh` (cron `*/5`) — 11 dimensions, alerts to TG with
  state-file throttle. See `deploy-ops.md`.
- `vera3-prune` — docker housekeeping.
- Each service has `restart: unless-stopped`.
- `usage_log` is append-only — never lost.

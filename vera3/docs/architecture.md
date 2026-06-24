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
              │  /events /sources /tokens /search  │
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
4. For each event: build prompt → call AIbroker `chat:fast` → parse JSON
   metadata (importance, topics, people, signals) → update row to `done`.
5. Embedding worker (same loop): one Voyage call per batch → vector to
   `embedding_voyage_3`.
6. `brain-search` queries events via FTS + cosine on demand.

## Triage queue scaling

`UPDATE … FOR UPDATE SKIP LOCKED` makes N workers race-safe. We run 1
replica by default (handles ~500/h). Burst with `--scale brain-triage=3`
when there's an import.

## Self-healing

- `vera3-monitor.sh` (cron `*/5`) — 11 dimensions, alerts to TG with
  state-file throttle. See `deploy-ops.md`.
- `vera3-prune` — docker housekeeping.
- Each service has `restart: unless-stopped`.
- `usage_log` is append-only — never lost.

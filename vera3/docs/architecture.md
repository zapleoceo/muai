# Architecture

## Services

```
              ┌───────────── ingestors ─────────────┐
              │  ingestor-telegram (userbot+tools)  │
              │  ingestor-gmail    (OAuth polling)  │
              │  ingestor-instagram (instagrapi)    │
              │  ingestor-trello   (REST polling)   │
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
| `vera3-ingestor-trello` | Trello actions-фид всех досок + суточный дайджест сроков |
| `vera3-dashboard` | HTMX UI on :8003 |
| `vera3-prune` | docker system prune --filter='until=72h' daily |

## Dashboard modules

`services/dashboard/src/dashboard/` follows one `APIRouter`-per-feature
file (was a single 1181-line `app.py` until 2026-07-11 — split to match
this project's own "~200 lines, one responsibility per file" convention):

| Module | Owns |
|---|---|
| `render.py` | Shared HTML chrome — `esc()`, `_render()`, page templates, favicon constants, and the auth-gate shortcuts (`owner_or_redirect`/`owner_or_blank_401`/`owner_or_auth_error`) plus small fragment builders (`row_list`, `data_table`, `freshness_pill`, `format_eta`, `local_dt`) used across route modules |
| `auth_routes.py` | `/login`, `/api/tg_login`, `/api/logout`, `/healthz` |
| `home_routes.py` | `/` — top-line stats cards |
| `progress_routes.py` | `/_progress`, `/control/backfill(-rate)` — HTMX live-progress fragment + pause/rate controls |
| `events_routes.py` | `/events` — triage log table |
| `sources_routes.py` | `/sources` — Gmail/Telegram/Instagram ingest health |
| `search_routes.py` | `/search-ui` — proxies to brain-search |
| `settings_routes.py` | `/settings`, `/control/settings` — SETTINGS registry |
| `entities_routes.py` | `/entities/duplicates`, `/entities/merge` |
| `gmail_oauth.py`, `instagram_login.py`, `telegram_login.py` | OAuth/login flows — the pattern the above split follows. `telegram_login.py` re-auths the userbot StringSession when a revoked session crash-loops the ingestor: `telegram_start_form` (GET `/api/telegram/start`) shows the phone form, `telegram_start` (POST) sends the code, `telegram_verify` (POST `/api/telegram/verify`) takes the code and — if cloud 2FA is on — the password, then saves the new encrypted session |
| `stats.py` | Cached (TTL 60s) DB aggregation feeding home/progress/sources |
| `auth.py` | Telegram Login Widget verification + signed session cookies |
| `app.py` | Just `FastAPI()` + `lifespan` + favicon routes + `include_router()` for all of the above |

### Timezone display

All DB datetime columns are **naive UTC** (ingestors write `datetime.utcnow()`;
`received_at`/`created_at` use `server_default=func.now()`). The dashboard
never `strftime`s a wall-clock time straight into HTML — every displayed
timestamp goes through `render.local_dt(dt, fmt)`, which emits
`<time data-utc="…Z" data-fmt="…">UTC-fallback</time>`. A small script in the
page footer (`_TZ_SCRIPT`) rewrites every such element into the **viewer's
browser timezone** on load and after each HTMX swap, and a footer note shows
the detected zone. The UTC text remains as a no-JS fallback and as the `title`
tooltip. Relative displays ("N мин назад", freshness pills) are UTC−UTC deltas
and are timezone-independent, so they are left as-is. Not covered: dates the
LLM echoes inside a search `answer` (generated server-side from UTC context,
no per-viewer zone available) and the fixed-UTC+7 month labels in
`brain-search/reports.py`.

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
`docker compose up -d --scale brain-triage=N`. `events` needs a partial
index for every `triage_status` the claim query filters on (`pending`,
`processing`, `error`) — see `brain.md`'s `ix_events_pending_claim` note
for what happens without one.

## brain-triage modules

`services/brain-triage/src/brain_triage/` (was an 820-line `worker.py`
until 2026-07-11 — split for the same reason as the dashboard above):

| Module | Owns |
|---|---|
| `config.py` | Env-configurable tuning constants (poll/batch/concurrency/pace, group-batch size/char cap, canonical chat_id SQL fragment) |
| `prompts.py` | Single-event + group-batch prompt templates |
| `schemas.py` | `TRIAGE_JSON_SCHEMA` / `TRIAGE_BATCH_JSON_SCHEMA` (strict json_schema defs) |
| `postprocess.py` | `NATURE_BY_SOURCE`/`PROJECT_VOCAB`/`postprocess_triage()` — validates the LLM's output against the closed vocab |
| `claim.py` | `_claim_batch()` (the `FOR UPDATE SKIP LOCKED` query), `chat_kind()`, group-batch chunking |
| `triage_calls.py` | `triage_one()`/`triage_group_batch()` — the actual broker calls; raise on failure, don't catch |
| `concurrency.py` | Semaphore-bounded wrappers normalizing single/group results to one shape |
| `project_override.py` | `apply_project_override()` — the deterministic `project_membership` fixup (own transaction, see domain-model.md) |
| `background_loops.py` | Watchdog (recover stuck `processing`) + retry-with-backoff (recover `error` → `dead`) |
| `worker.py` | `process_pending()` orchestration (claim → embed → dispatch → write, three deliberately-separate transactions) + `main_loop()` |

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

# Vera 2.0 — Project Documentation

> **READ THIS FIRST.** This file is the single source of truth for any AI assistant,
> developer, or tool working on this project. Before touching any code, read this
> document fully. It describes architecture, conventions, deployment, and rules.

---

## Quick facts

| Item | Value |
|------|-------|
| Stack | Python 3.12, FastAPI, aiogram 3, SQLAlchemy 2 async, Telethon, SQLite |
| Live URL | https://dima.veranda.my |
| Server | Hetzner VPS, 195.201.31.49, SSH alias `hetzner-root` |
| Repo | https://github.com/zapleoceo/muai |
| DB | SQLite at `DB_PATH=/data/vera.db` (single shared file, all services) |
| Project dir on server | `/var/www/vera` |
| Owner Telegram ID | `169510539` (Dima) |

---

## Concept

Vera is an AI orchestrator. It does **not** execute tasks itself. It:

1. Receives tasks from Dima via @mention in a private Telegram group
2. Classifies intent via a cheap prefilter
3. Reformats the task into optimized prompts for each relevant specialized bot
4. Dispatches to bots in parallel via internal HTTP
5. Evaluates response quality with an LLM-judge
6. Retries with improved prompts if quality is poor (max 3 attempts)
7. Posts the final result to the group (Telegram = audit trail)

New bots can be added dynamically by telling Vera in the group. Vera instructs vera-dev to scaffold, vera-git to push, and the CI/CD pipeline deploys automatically.

---

## Architecture

```
Dima (Telegram group)
        │
        ▼
  vera-core (orchestrator)
        │
        ├── prefilter (classify intent, pick bots)
        │
        ├── dispatcher ──────────────────────────────────┐
        │       │                                         │
        │       ▼                                         ▼
        │  vera-telegram        vera-dev        vera-git  vera-monitor ...
        │  (Telethon tools)  (code + Claude) (GitHub ops) (group watch)
        │       │
        │       └─── internal HTTP (fast) + Telegram group (audit trail)
        │
        ├── evaluator (LLM-judge, score 0–1)
        └── retry (optimize prompt, re-dispatch, max 3x)
```

### Communication model

| Channel | Purpose |
|---------|---------|
| Internal HTTP (`http://vera-*:8001`) | Task dispatch and results (fast path) |
| Telegram group | Audit trail visible to Dima, @mention entry point |
| SQLite DB | Shared state: tokens, agents registry, task log, settings |

---

## Project structure

Every file has one responsibility. Max ~80 lines per file.

```
vera/
├── CLAUDE.md                    ← this file
├── docker-compose.yml           ← all services
├── .env                         ← minimal secrets only (no tokens)
├── .github/
│   └── workflows/
│       └── deploy.yml           ← CI/CD, triggers on push to master
│
├── shared/                      ← pip-installable shared library
│   ├── pyproject.toml
│   └── vera_shared/
│       ├── __init__.py
│       ├── db/
│       │   ├── engine.py        ← SQLite async engine factory
│       │   ├── models.py        ← all ORM models
│       │   └── migrations.py    ← create_all on startup
│       ├── tokens/
│       │   ├── model.py         ← Token dataclass + capabilities
│       │   ├── repository.py    ← DB CRUD for tokens
│       │   ├── pool.py          ← TokenPool: rotation, cooldown
│       │   └── selector.py      ← get_token(capability) → token
│       ├── providers/
│       │   ├── base.py          ← BaseProvider ABC
│       │   ├── gemini.py        ← GeminiProvider(BaseProvider)
│       │   ├── deepseek.py      ← DeepSeekProvider(BaseProvider)
│       │   ├── voyage.py        ← VoyageProvider(BaseProvider)
│       │   └── registry.py      ← {provider_name: BaseProvider}
│       ├── registry/
│       │   ├── model.py         ← AgentRecord dataclass
│       │   ├── repository.py    ← DB CRUD for agents
│       │   └── client.py        ← register_self(), heartbeat()
│       └── base_bot/
│           ├── bot.py           ← BaseBot: handle_task(), register()
│           ├── task.py          ← Task, TaskResult dataclasses
│           └── server.py        ← FastAPI server for each bot
│
├── vera-core/                   ← orchestrator
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py              ← FastAPI + aiogram lifespan
│       ├── config.py            ← Settings from .env
│       ├── bot/
│       │   ├── handler.py       ← incoming TG messages → orchestrator
│       │   └── sender.py        ← send_to_group(), reply()
│       ├── orchestrator/
│       │   ├── pipeline.py      ← main flow: prefilter→dispatch→evaluate→retry
│       │   ├── prefilter.py     ← classify intent, pick bots
│       │   ├── dispatcher.py    ← parallel HTTP calls to bots
│       │   ├── evaluator.py     ← LLM-judge: score response quality
│       │   ├── retry.py         ← retry loop with prompt optimization
│       │   └── prompt_builder.py← format task per bot capabilities
│       ├── deploy/
│       │   ├── endpoint.py      ← POST /deploy webhook
│       │   ├── runner.py        ← git pull + docker compose
│       │   └── health.py        ← poll /health, collect logs
│       └── dashboard/
│           ├── routes.py        ← /api/* endpoints
│           └── auth.py          ← Telegram login widget
│
├── vera-telegram/               ← Telethon userbot
│   ├── Dockerfile
│   └── app/
│       ├── main.py
│       ├── bot.py               ← BaseBot implementation
│       ├── tools/
│       │   ├── read_messages.py ← get_messages(peer, limit)
│       │   ├── search_dialogs.py← search_dialogs(query)
│       │   └── send_message.py  ← send_message(peer, text)
│       └── userbot/
│           ├── client.py        ← Telethon client singleton
│           └── session.py       ← session file management
│
├── vera-dev/                    ← code development bot (Claude)
│   ├── Dockerfile
│   └── app/
│       ├── main.py
│       ├── bot.py
│       └── tools/
│           ├── read_file.py
│           ├── write_file.py
│           ├── run_tests.py
│           └── trigger_deploy.py
│
├── vera-git/                    ← GitHub operations bot
│   ├── Dockerfile
│   └── app/
│       ├── main.py
│       ├── bot.py
│       └── tools/
│           ├── push.py          ← git add, commit, push
│           ├── pr.py            ← create/merge PRs
│           └── status.py        ← git status, log
│
├── vera-monitor/                ← TG group monitoring
│   ├── Dockerfile
│   └── app/
│       ├── main.py
│       ├── bot.py
│       ├── listener.py          ← Telethon event handler
│       ├── dispatch.py          ← group_msg/mention/reply routing
│       └── summarizer.py        ← Gemini Flash summary for mentions
│
└── dashboard/                   ← web UI (static)
    └── index.html               ← SPA: tokens, agents, tasks, deploy log
```

---

## Environment variables (`.env`)

Tokens and API keys are **never** in `.env`. They live only in the SQLite DB.

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN_VERA=        # vera orchestrator bot
TELEGRAM_BOT_TOKEN_DEV=         # vera-dev bot
TELEGRAM_API_ID=                # userbot (Telethon)
TELEGRAM_API_HASH=              # userbot
OWNER_TELEGRAM_ID=169510539     # Dima's TG id
VERA_GROUP_ID=                  # private group chat_id

# Infrastructure
DB_PATH=/data/vera.db
SESSION_SECRET=
DEPLOY_SECRET=                  # for /deploy endpoint
WEBHOOK_BASE_URL=https://dima.veranda.my

# GitHub (for vera-git bot)
GITHUB_TOKEN=
GITHUB_REPO=zapleoceo/muai
```

---

## Database schema

Single SQLite file at `DB_PATH`. All services share it. Schema applied via `create_all` on startup (no Alembic).

```sql
-- All API keys. Tokens NEVER leave the DB.
tokens (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,          -- gemini | deepseek | voyage | anthropic
  label TEXT NOT NULL,             -- human name (zapleosoft, default, etc)
  token TEXT NOT NULL,             -- actual key — NEVER in git or env
  capabilities JSON NOT NULL,      -- ["chat:fast", "prefilter"]
  is_active BOOLEAN DEFAULT 1,
  daily_limit INTEGER DEFAULT 1500,
  daily_used INTEGER DEFAULT 0,
  daily_reset_at DATE,
  cooldown_until DATETIME,
  error_count INTEGER DEFAULT 0,
  last_used_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)

-- Registered bots, updated via heartbeat
agents (
  id TEXT PRIMARY KEY,             -- "vera-telegram", "vera-gmail-dima"
  name TEXT NOT NULL,
  capabilities JSON NOT NULL,      -- what this bot can do: ["email:read"]
  required_caps JSON NOT NULL,     -- what token caps it needs: ["chat:fast"]
  http_url TEXT NOT NULL,          -- internal http://vera-telegram:8001
  bot_username TEXT,               -- @vera_telegram_bot
  status TEXT DEFAULT 'offline',   -- online | offline | busy
  last_heartbeat DATETIME,
  registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
)

-- Every orchestration task, fully logged
tasks (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- telegram_group | direct | scheduled
  user_id INTEGER,
  input_text TEXT NOT NULL,
  intent JSON,                     -- prefilter result
  agents_used JSON,                -- which bots were called
  attempts INTEGER DEFAULT 1,
  final_result TEXT,
  quality_score REAL,
  tokens_used JSON,                -- per-provider usage stats
  cost_usd REAL DEFAULT 0,
  duration_ms INTEGER,
  status TEXT DEFAULT 'pending',   -- pending | running | done | failed
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)

-- Key-value config blobs
settings (
  key TEXT PRIMARY KEY,
  value JSON NOT NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

---

## Token capability system

Tokens are selected by **capability flag**, not by provider name. This means new providers can be added by inserting tokens with the right capability — no code changes needed.

### Capability flags

| Flag | Meaning | Default providers |
|------|---------|------------------|
| `chat:fast` | Cheap/fast chat | Gemini Flash |
| `chat:smart` | Stronger reasoning | DeepSeek, Anthropic |
| `chat:code` | Code generation | DeepSeek, Anthropic |
| `embed` | Text embeddings | Voyage |
| `prefilter` | Cheapest classifier, always runs first | Gemini Flash |

### Default assignments by provider

| Provider | Default capabilities |
|---------|---------------------|
| `gemini` | `["chat:fast", "prefilter"]` |
| `deepseek` | `["chat:smart", "chat:code"]` |
| `voyage` | `["embed"]` |
| `anthropic` | `["chat:smart", "chat:code"]` |

### Existing tokens (migrate from Vera 1.0 SQLite DB)

| Provider | Count | Labels |
|---------|-------|--------|
| Gemini Flash (free tier) | 8 | default, zapleosoft, Verandapayments, Zaporozhets, Liza, Billaa, Maya, Oleg |
| DeepSeek | 1 | demoniwwwe |
| Voyage (embeddings) | 5 | demoniwwwe, zapleosoft, verandapay, lev, Eva |
| Anthropic Claude | 1 | default |

All 15 tokens must be exported from the old DB and imported into the new `tokens` table with appropriate `capabilities`. They must **never** appear in `.env` or git.

---

## Token rotation logic (`shared/tokens/pool.py`)

```
get_token(capability: str) → str:
  1. Filter tokens WHERE capability IN capabilities
  2. Filter is_active = True
  3. Filter cooldown_until < now OR cooldown_until IS NULL
  4. Filter daily_used < daily_limit
  5. Sort by last_used_at ASC  (least recently used first)
  6. If empty → find minimum cooldown_until, raise TokensExhausted(retry_after=X)
  7. Return token, update last_used_at, increment daily_used

on_error(token_id, error_type):
  429        → cooldown 60s,  error_count++
  5xx        → cooldown 300s, error_count++
  auth_error → is_active = False, alert posted to VERA_GROUP_ID
```

Daily counters reset at midnight. On process restart, cooldowns reset (in-memory state). Daily usage is persisted to DB and survives restarts via `daily_used` column.

---

## Orchestration pipeline (`vera-core/app/orchestrator/pipeline.py`)

```
Incoming message (@vera mention or direct task)
        │
        ▼
prefilter.py  ← cheapest token (prefilter cap), classify intent, pick target bots
        │
        ▼
prompt_builder.py  ← reformat task as optimized prompt per bot's capabilities
        │
        ▼
dispatcher.py  ← parallel asyncio HTTP calls to selected bots
        │
        ▼
evaluator.py  ← LLM-judge scores each response (0.0–1.0)
        │
        ├── score ≥ 0.7 → done, post result to group
        │
        └── score < 0.7 → retry.py (max 3 attempts total)
                              ← optimize prompt, re-dispatch, re-evaluate
```

---

## Health check (every service)

Every bot service exposes:

- `GET /health` → `{"status": "ok", "service": "vera-telegram", "version": "..."}`
- `GET /metrics` → basic stats (tasks handled, token usage, uptime)

Health checks are polled by `vera-core/app/deploy/health.py` for 60 seconds after each deploy.

---

## Auto-deploy flow

1. vera-git bot pushes code to GitHub (`zapleoceo/muai`, branch `master`)
2. GitHub Actions triggers on push to master
3. Actions calls `POST /deploy` on server with `DEPLOY_SECRET` header
4. Server runs: `git pull → docker compose build → docker compose up -d → health check`
5. Health check polls `/health` on each service for up to 60s
6. Result (success/failure + last 20 log lines) is posted to `VERA_GROUP_ID` Telegram group
7. Dima can also trigger manually: `@vera задеплой`
8. vera-dev triggers deploy after code changes and waits for the result

### GitHub Actions (`.github/workflows/deploy.yml`)

```yaml
on:
  push:
    branches: [master]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger deploy
        run: |
          curl -X POST https://dima.veranda.my/deploy \
            -H "Authorization: Bearer ${{ secrets.DEPLOY_SECRET }}" \
            -H "Content-Type: application/json" \
            -d '{"ref": "${{ github.sha }}", "message": "${{ github.event.head_commit.message }}"}'
```

### Manual deploy

```bash
ssh hetzner-root "cd /var/www/vera && git pull && docker compose build && docker compose up -d"
```

---

## Adding a new bot (dynamic)

1. Dima: `@vera добавь бота для Notion`
2. Vera asks vera-dev to scaffold from `BaseBot` template
3. vera-dev creates `vera-notion/` with correct structure
4. vera-git commits and pushes to GitHub (`master`)
5. Auto-deploy triggers, new container starts
6. vera-notion calls `register_self()` → writes row to `agents` table
7. Vera announces in group: `✅ vera-notion готов: notion:read, notion:write`

Every bot uses `shared/base_bot/bot.py` (`BaseBot`) and `shared/registry/client.py` (`register_self`, `heartbeat`). No changes to vera-core needed.

---

## Key runtime singletons

| Singleton | Module | Purpose |
|-----------|--------|---------|
| `get_engine()` | `vera_shared.db.engine` | SQLite async engine |
| `get_token_pool()` | `vera_shared.tokens.pool` | Token rotation + cooldown |
| `get_agent_registry()` | `vera_shared.registry.repository` | Agent CRUD |
| `get_client()` | `vera_telegram.userbot.client` | Telethon TelegramClient |

Pattern for all singletons:

```python
_pool: TokenPool | None = None

def get_token_pool() -> TokenPool:
    global _pool
    if _pool is None:
        _pool = TokenPool()
    return _pool
```

---

## Migration from Vera 1.0

| Item | Action |
|------|--------|
| 15 API tokens | Export from old SQLite DB, import into new `tokens` table with `capabilities` |
| Telethon session | Keep at `/data/sessions/userbot.session` — same path, do not recreate |
| Old tasks data | Discard — new schema is incompatible |
| `.env` API keys | Remove all `*_API_KEY` vars from `.env` after DB import |

---

## Code conventions

### Python
- Python 3.12, `async` everywhere — all DB, HTTP, file I/O
- One file = one responsibility, max ~80 lines per file
- Layer order: `routes` → `services` → `repository` → `models`
- No business logic in routes; no DB access outside repository layer
- Singletons: module-level `_var: Type | None = None` + `get_var() -> Type`
- Type hints on all function signatures; use `X | None` not `Optional[X]`
- Use `list[X]` / `dict[K, V]` not `List` / `Dict`
- No bare `except:`; no swallowed exceptions
- No comments explaining what code does — names do that
- Add a comment only when WHY is non-obvious (workaround, constraint, invariant)
- No docstrings longer than one line; prefer none
- `asyncio.Lock` for shared mutable state
- `async with AsyncSessionLocal() as session:` — never reuse sessions across calls
- Always commit explicitly; never rely on implicit commit

### HTML / JS (dashboard/index.html)
- Single file, vanilla JS, no build step, no frameworks
- All API calls via `fetch`; handle 401 → show login screen
- State in module-level `let` variables, never in the DOM

### Git
- Commit style: `feat:`, `fix:`, `refactor:`, `chore:`
- One logical change per commit
- Never commit `.env`, `*.session`, or tokens of any kind

---

## Security rules

- Tokens are stored **only** in the SQLite DB — never in `.env`, never in git
- All admin routes require `Depends(require_owner)` — never relax this
- Deploy endpoint requires `Authorization: Bearer <DEPLOY_SECRET>` header
- Session cookies are HMAC-signed via `SESSION_SECRET`
- `OWNER_TELEGRAM_ID` gates admin dashboard; never accept user-supplied IDs
- Verify Telegram Login Widget hash before granting session
- Never log full message content at INFO level — use DEBUG
- Never run `DELETE FROM` without a `WHERE` clause on production tables
- Confirm any `DROP` or `TRUNCATE` with Dima before executing

---

## Adding a new feature — checklist

1. DB change? → add model field in `vera_shared/db/models.py`; `create_all` handles it on next restart (no Alembic needed — SQLite)
2. Business logic → new function in the relevant service module
3. API endpoint → thin route, call the service, return result
4. Register router in `main.py` if new file
5. Frontend → update `dashboard/index.html`
6. Commit, push → auto-deploy triggers

---

## Known limitations / gotchas

- **SQLite concurrency** — SQLite allows one writer at a time. All writes go through the shared async engine with WAL mode enabled. Do not bypass the engine with raw connections.
- **Daily token counters** — `daily_used` is persisted to DB but cooldown state is in-memory. A restart resets cooldowns (next request re-learns them via 429 errors).
- **Telethon session** — `userbot.session` must not be deleted or recreated. It survives restarts via volume mount at `/data/sessions/`.
- **iter_dialogs() flood waits** — Telegram imposes GetHistory flood waits; large accounts can cause slow syncs. This is expected behavior.
- **Cloudflare Flexible SSL** — traffic between Cloudflare and server is HTTP. If switching to Full SSL, nginx must listen on 443 with a certificate.
- **Port 443** — occupied by MTProxy. nginx must NOT use 443.
- **docker.sock** — mounted only for the deploy endpoint. Do not expose it further.

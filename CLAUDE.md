# myAI — Project Documentation

> **READ THIS FIRST.** This file is the single source of truth for any AI assistant,
> developer, or tool working on this project. Before touching any code, read this
> document fully. It describes architecture, conventions, deployment, and rules.

---

## Quick facts

| Item | Value |
|------|-------|
| Stack | Python 3.12, FastAPI, aiogram 3, SQLAlchemy 2 async, Telethon, PostgreSQL 16 |
| Bot token | in `.env` → `TELEGRAM_BOT_TOKEN` |
| Live URL | https://dima.veranda.my |
| Server | Hetzner VPS, 195.201.31.49, port 9617, SSH alias `hetzner-root` |
| Repo | https://github.com/zapleoceo/muai |
| Container | `tgbot-bot-1` (bot + FastAPI), `tgbot-db-1` (PostgreSQL) |
| Project dir on server | `/var/www/tgbot` |
| Compose file on server | `/var/www/tgbot/docker-compose.yml` |

---

## Architecture

```
myAI/
├── app/
│   ├── main.py              # FastAPI app, lifespan (DB init, token seed, webhook, userbot)
│   ├── config.py            # Pydantic Settings, reads .env
│   ├── api/
│   │   ├── auth.py          # /auth/telegram login, /auth/logout, require_owner dependency
│   │   ├── admin.py         # /api/admin/stats|logs|deploy|migrate + token CRUD
│   │   ├── chats.py         # /api/admin/chats CRUD, sync stop/status, global settings
│   │   └── routes.py        # misc public routes
│   ├── bot/
│   │   ├── handlers/
│   │   │   ├── commands.py  # /start, /help, /ask
│   │   │   └── messages.py  # incoming message handler → LLM → reply
│   │   └── storage.py       # save_incoming(), save_outgoing(), get_dialog_context()
│   ├── db/
│   │   ├── database.py      # AsyncEngine + AsyncSessionLocal
│   │   ├── models.py        # SQLAlchemy ORM models (see schema section)
│   │   └── repository.py    # MessageRepo: upsert_chat, upsert_user, save_message, etc.
│   ├── llm/
│   │   ├── base.py          # LLMProvider ABC
│   │   ├── factory.py       # get_llm_provider() singleton — reads LLM_PROVIDER from env
│   │   ├── gemini_provider.py  # GeminiProvider: uses TokenManager for key rotation
│   │   ├── openai_provider.py  # OpenAIProvider (also used for Groq via base_url)
│   │   └── stub.py          # StubProvider: echoes input, used when no keys configured
│   ├── services/
│   │   ├── auth.py          # make_token(), verify_token(), verify_telegram_widget()
│   │   ├── chat_settings.py # per-chat sync config + global sync settings (type filter, blacklist)
│   │   ├── deploy.py        # get_logs(), run_migration(), run_deploy() — runs docker commands
│   │   ├── stats.py         # get_dashboard_stats() — SQL aggregates + DB size
│   │   ├── sync_manager.py  # SyncManager singleton: cancellation set, task ref, progress
│   │   └── tokens.py        # TokenManager singleton: rotation, cooldown, daily counter
│   └── userbot/
│       ├── client.py        # TelegramClient singleton, start_userbot(), stop_userbot()
│       ├── handlers.py      # Telethon event handlers (live messages → storage)
│       ├── media.py         # chat_type(), chat_title(), media_type() helpers
│       ├── storage.py       # save_event(), save_history_message()
│       └── sync.py          # sync_history(): iterates dialogs, respects ChatSyncConfig
├── alembic/
│   ├── versions/
│   │   ├── 001_initial.py   # chats, tg_users, messages, settings tables
│   │   ├── 002_api_tokens.py
│   │   └── 003_chat_sync_config.py
│   └── env.py
├── static/
│   └── index.html           # Single-page dashboard (3 tabs: Dashboard / Chats / Settings)
├── scripts/
│   └── auth_userbot.py      # One-time Telethon session auth (run interactively)
├── nginx/
│   └── tgbot.conf           # nginx: listen 80 only (Cloudflare Flexible SSL)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                     # secrets — NEVER commit
└── CLAUDE.md                # ← you are here
```

---

## Database schema

```
chats           id(PK), type, title, created_at
tg_users        id(PK), username, first_name, last_name, language_code, is_bot, created_at
messages        id(PK), chat_id(FK), user_id(FK), telegram_msg_id, direction(in/out),
                text, media_type, file_id, caption, raw_json, date_utc,
                reply_to_msg_id, is_auto_reply, via_guest_bot, edit_date, dialog_key
settings        key(PK), value   ← JSON blobs for global config (key="sync_settings")
api_tokens      id(PK), provider, token, label, capabilities(JSONB), is_active, created_at, last_used_at, error_count
chat_sync_config chat_id(PK/FK→chats), enabled, depth_days, approved_at, skip_reason, created_at
```

**Applying migrations:** The container has no psycopg2, so Alembic can't run directly.
Apply SQL manually via:
```bash
docker compose exec -T db psql -U bot tgbot -c "CREATE TABLE ..."
```

---

## Key runtime singletons

| Singleton | Module | Purpose |
|-----------|--------|---------|
| `get_llm_provider()` | `app.llm.factory` | LLM backend (Gemini/OpenAI/Stub) |
| `get_token_manager()` | `app.services.tokens` | API key rotation + daily limit tracking |
| `get_sync_manager()` | `app.services.sync_manager` | Sync task ref + per-chat cancellation |
| `get_client()` | `app.userbot.client` | Telethon TelegramClient |

All singletons are initialized in `app/main.py` lifespan, in this order:
1. DB `create_all`
2. TokenManager: `seed_from_env` → `load`
3. `auto_approve_existing_chats`
4. Webhook registration
5. `start_userbot` (which starts sync as a background task)

---

## Sync logic (`app/userbot/sync.py`)

Flow per dialog:
1. Check `type_allowed` (global setting: private/group/supergroup/channel)
2. Check `is_blacklisted` (global setting: list of chat_ids or @usernames)
3. Look up `ChatSyncConfig`:
   - **No config** → register as `pending`, skip
   - **enabled=False** → skip
4. Check `SyncManager._cancelled` — if chat was cancel-requested, skip
5. Sync messages since `depth_days` (per-chat override or global default)
6. Check `_cancelled` every 50 messages mid-loop for live cancellation

**Important:** When a user leaves a group in Telegram, `iter_dialogs()` simply won't
return it. Sync skips it naturally. Existing messages stay in the DB.

---

## Token rotation (`app/services/tokens.py`)

- Round-robin across active `_Slot` objects (in-memory)
- Tokens have **capabilities** (JSON array in DB): `chat`, `embed`
  - If `capabilities` is NULL, defaults are derived from `provider`:
    - `gemini`: `chat, embed`
    - `openai`: `chat, embed`
    - `deepseek`: `chat`
    - `groq`: `chat`
- **429 response** → 60s cooldown on that slot
- **Other error** → 5 min cooldown
- **Daily limit** → 1500 req/day per key (Gemini free tier); slot marked unavailable
- Daily counter resets at midnight (local server time, checked on each `available()` call)
- On container restart: cooldowns and daily counters reset (in-memory only)
- Counters shown in Settings tab with a progress bar (green→yellow→red at 70%/90%)

---

## Deployment

### Auto-deploy (no SSH needed)
POST to `/api/admin/deploy` with `Authorization: Bearer <DEPLOY_SECRET>`:
```
deploy.py → git -C /var/www/tgbot pull → docker compose build bot → docker compose up -d bot
```
GitHub Actions workflow (`.github/workflows/deploy.yml`) triggers this on push to `master`.

### Manual deploy (with SSH)
```bash
ssh hetzner-root "cd /var/www/tgbot && git pull && docker compose build bot && docker compose up -d bot"
```

### Apply a new migration
```bash
ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml exec -T db psql -U bot tgbot -c 'SQL HERE'"
```

### View live logs
```bash
ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml logs -f --tail=50 bot"
```

---

## Infrastructure notes

- **SSL**: Cloudflare Flexible — origin speaks HTTP on port 80 only
- **Port 443**: occupied by MTProxy (Telegram proxy), nginx must NOT touch it
- **nginx config**: `nginx/tgbot.conf` — listen 80, proxy_pass to 127.0.0.1:8000, injects `X-Forwarded-Proto: https`
- **Sessions volume**: `/app/sessions/userbot.session` — Telethon session file, never delete
- **DB volume**: `tgbot_pgdata` — postgres data, survives container recreates
- **docker.sock mount**: required for deploy endpoint to run docker commands from inside the container

---

## Code style rules

> These apply to every file in this project. Follow them strictly.

### Python
- **No god files.** Each file has one responsibility. Max ~200 lines; split if larger.
- **No comments explaining what code does.** Names are the documentation.
  Add a comment only when WHY is non-obvious (workaround, constraint, invariant).
- **No docstrings** longer than one line. Prefer none at all.
- **Services layer** for all business logic — API routes are thin (validate input, call service, return result).
- **Repository layer** for all DB access — no raw SQL in routes or services except `stats.py` aggregates.
- **Singletons** via module-level `_var: Type | None = None` + `get_var() -> Type` pattern.
- **Async everywhere**: all DB calls, HTTP calls, file I/O must be async.
- **No backwards-compat shims**: if something is removed, remove it completely.
- **No feature flags**: change the code, don't wrap it.
- Type hints on all function signatures. Use `X | None` not `Optional[X]`.

### HTML / JS (static/index.html)
- Single file, vanilla JS. No build step, no frameworks.
- All API calls via `fetch`. Handle 401 → show login screen.
- State in module-level `let` variables, never in the DOM.
- Keep JS functions short and named for what they do.

### Git
- Commit messages: `type: short description` (feat/fix/refactor/chore)
- One logical change per commit.
- Never commit `.env` or session files.

---

## Environment variables (`.env`)

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | aiogram bot token |
| `WEBHOOK_URL` | full URL for Telegram webhook |
| `WEBHOOK_SECRET` | optional webhook validation token |
| `TELEGRAM_API_ID` | Telethon userbot |
| `TELEGRAM_API_HASH` | Telethon userbot |
| `TELEGRAM_PHONE` | Telethon userbot phone number |
| `SYNC_HISTORY_DAYS` | default sync depth on startup |
| `DB_URL` | asyncpg connection string |
| `DB_PASSWORD` | postgres password (also used by docker-compose) |
| `LLM_PROVIDER` | `gemini-2.5-flash` \| `gemini-2.5-pro` \| `openai` \| `groq` \| `stub` |
| `GEMINI_API_KEY` | seeded into DB on first start if no `gemini` tokens exist |
| `OPENAI_API_KEY` | seeded into DB on first start if no `openai` tokens exist |
| `GROQ_API_KEY` | seeded into DB on first start if no `groq` tokens exist |
| `SESSION_SECRET` | HMAC key for dashboard auth cookies |
| `DEPLOY_SECRET` | Bearer token for `/api/admin/deploy` |
| `TELEGRAM_BOT_USERNAME` | used in Telegram Login Widget |
| `BOT_MODE` | `manual` (reply only when asked) or `auto` |
| `LOG_LEVEL` | `INFO` \| `DEBUG` \| `WARNING` |
| `OWNER_TELEGRAM_ID` | your Telegram user ID — grants dashboard access |

---

## Adding a new feature — checklist

1. DB change? → add model field in `app/db/models.py` + write migration SQL
2. Business logic → new function in `app/services/`
3. API endpoint → thin route in `app/api/`, call the service
4. Register router in `app/main.py` if new file
5. Frontend → update `static/index.html`
6. Commit, push → GitHub Actions auto-deploys

---

## Known limitations / gotchas

- **Alembic can't run in container** — no psycopg2, only asyncpg. Apply migrations via psql directly.
- **Daily token counters reset on restart** — they're in-memory. A restart loses today's count.
- **`iter_dialogs()` flood waits** — Telegram imposes GetHistory flood waits; sync can take hours for large accounts. This is expected.
- **Cloudflare Flexible SSL** — all traffic between Cloudflare and the server is HTTP. If you ever switch to Full SSL, nginx must listen on 443 with a certificate.
- **MTProxy on port 443** — do not move it. It serves Telegram users as a proxy; stopping it breaks connectivity for those users.

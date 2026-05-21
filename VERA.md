# Vera 2.0 — Single Source of Truth

> Everything about the project lives here. CLAUDE.md and docs/ARCHITECTURE.md
> are now stubs pointing to this file. Update this — not them.

---

## 1. Vision

Vera is an **AI brain that decides, not a menu generator**.

She receives signals from many sources (Gmail, Telegram, bank alerts, Instagram,
Facebook, …), records them as episodes in a bi-temporal knowledge graph
(Graphiti + Neo4j), and **picks one action per event** with a self-assessed
confidence score:

| Confidence | Behaviour |
|---|---|
| **≥ 0.95** | Executes immediately. Sends Dima a post-fact card with [✋ Откати] |
| **0.50 — 0.95** | Proposes 1-3 actions in a card. Dima clicks or replies free-text |
| **< 0.50** | Silent notification card, no buttons |

Dima only corrects errors. Silence = nothing happens (no implicit approval).
Every explicit 👍 / ✋ / ✍️ is written back to Graphiti as an annotation
episode and surfaces in retrieval for the next similar event.

**Hard rules:**

- Source-agnostic core — everything flows through one /event endpoint
- MCP-first integrations — community MCP > custom adapter
- Tools, not commands — Vera composes tool calls; no hardcoded if-else
- All decisions are training data
- No hallucination — args come from event metadata, not LLM imagination
- Owner-only authority — `OWNER_TELEGRAM_ID` is the single privileged identity

---

## 2. Live infra

| Item | Value |
|---|---|
| Live URL | https://dima.veranda.my |
| Server | Hetzner VPS, SSH alias `hetzner-root`, port 9617 |
| Project dir on server | `/var/www/vera` |
| DB | SQLite (WAL) at `/data/vera.db` — single file, all services |
| Graph | Neo4j Aura Free |
| Owner Telegram ID | `169510539` |
| Bot username | `@vera_lifemind_bot` (token in env) |

---

## 3. Service topology

```
                          ┌─────────────────────────────┐
                          │  vera-core (FastAPI+aiogram) │
                          │  - /bot/webhook              │
                          │  - /event ingest             │
                          │  - dashboard /api/*          │
                          │  - orchestrator + triage     │
                          │  - MCP client manager (stdio)│
                          └────────────┬─────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
   ┌────────▼─────────┐      ┌─────────▼────────┐       ┌─────────▼─────────┐
   │  vera-telegram   │      │   vera-gmail     │       │  MCP children     │
   │  - Telethon      │      │  - OAuth poll    │       │  spawned in-proc: │
   │  - tools/* HTTP  │      │  - tools/* HTTP  │       │  fetch, git,      │
   │  - source poller │      │                  │       │  github, ...      │
   └──────────────────┘      └──────────────────┘       └───────────────────┘
```

Vera-git and vera-web were removed once MCP equivalents (fetch, git, github)
covered their functionality.

---

## 4. Core domain models

### `sources` — configurable event sources
Per-source filter rules + poll interval + base threshold. Filter format:

```json
[
  {"match": {"chat_type": "private"}, "action": "include"},
  {"match": {"chat_id_not_in": [-100123]}, "action": "exclude"},
  {"match": {"mention_me": true}, "action": "priority"}
]
```

Last matching rule wins, default `exclude`. Predicates: `chat_type`,
`chat_id`/`chat_id_not_in`, `from_user_id`, `from_username`, `from_contact_known`,
`mention_me`, `reply_to_me`, `text_contains`, `text_regex`, `from_contains`,
`subject_contains`, `time_of_day_between`, `has_attachment`.

### `events` — every triggered signal
`source`, `category`, `content_text`, `entity_hints[]`, `metadata{}`,
`triage_status` (pending → awaiting_user → decided/executed/auto_executed),
`triage_result` (LLM proposal + user_choice + executions[]).

### `mcp_servers` — runtime-registered MCP children
Stdio subprocess specs. `command[]`, `env{}`, `enabled`, status, `tools_count`.
Lifecycle: `app.mcp.manager.refresh_from_db()` at startup + on dashboard change.

### `tokens` — encrypted LLM/provider keys
LiteLLM router builds a model list dynamically from this table.
Capabilities: `chat:fast`, `chat:smart`, `chat:code`, `embed`, `prefilter`.

### `gmail_accounts` — OAuth refresh tokens (encrypted)
### `agents` — registered HTTP tool providers (vera-telegram, vera-gmail)
### `settings` — kv blob (e.g. `persona` digest)

---

## 5. Event flow (the brain loop)

```
External signal (Gmail/Telegram poller / webhook)
    │
    ▼
POST /event   (X-Internal-Secret required)
    │
    ▼
save_event   →  schedule_ingest (background)
                    │
                    ├─ Graphiti add_episode (30s timeout, won't block)
                    │
                    ▼
                schedule_triage
                    │
                    ▼
            Graphiti retrieval (related episodes across ALL sources)
                    │
                    ▼
            LLM (chat:fast) with persona + tools + context
                    │
                    ▼
            Decision(action, alternatives[], confidence)
                    │
                ┌───┴───┐
                │       │
              ≥0.95    <0.95
                │       │
              auto-   card with buttons
              execute  + free-text path
```

Cards: `vera-core/app/triage/card.py`. Callback handler: `bot/callbacks.py`
(owner-only, group-bound, calls `record_user_decision` then `call_tool`).

---

## 6. Tool registry

Unified via `app/orchestrator/tool_router.py:collect_tools()`:
1. HTTP agents — `Agent` rows with `tools[]`, last heartbeat <5min ago
2. MCP children — `mcp.manager.get_routed_tools()`

HTTP wins on name collision. Tool spec: `{name, description, params:[{name,type,description,required,default}]}`.

Destructive tools have **server-side arg resolution**
(`tool_router._resolve_safe_args`) — e.g. `gmail_send_reply.to` is overridden
with the actual last-sender from the thread. Prevents prompt-injection
from email body changing recipient.

---

## 7. Security invariants (the review-driven shortlist)

| Boundary | Enforcement |
|---|---|
| `/internal/agents/register` | X-Internal-Secret + host allowlist |
| `/internal/agents` GET | `Depends(require_owner)` |
| `/api/admin/*` | `Depends(require_owner)` |
| `/api/sources`, `/api/mcp/*`, `/api/persona/*` | `Depends(require_owner)` |
| `/event` | X-Internal-Secret (shared by pollers) |
| `/bot/webhook` | aiogram secret header |
| `triage_callback` | from_user.id == OWNER_TELEGRAM_ID + chat.id == VERA_GROUP_ID |
| Destructive tool args | server-side resolution overrides LLM choice |
| Tokens at rest | AES-CTR + HMAC via `crypto.py` |
| Deploy webhook | DEPLOY_SECRET bearer |

---

## 8. Deploy (CI/CD)

`.github/workflows/deploy.yml` triggers on push to master:

1. webfactory/ssh-agent — SSH key into runner
2. ssh-keyscan port 9617 (NOT 22)
3. `flock /tmp/vera-deploy.lock` — serialise concurrent deploys
4. `git fetch + git reset --hard origin/master` (no merge surprises)
5. `docker compose build --pull && up -d --remove-orphans`
6. Smoke ping `https://dima.veranda.my/` (5 attempts × 5s)
7. `docker exec vera-vera-core-1 pytest /app/tests -q`

Manual: `ssh hetzner-root "cd /var/www/vera && git pull && docker compose build && docker compose up -d"`

---

## 9. MCP presets (one-click in dashboard)

| ID | Package | Env required |
|---|---|---|
| fetch | `uvx mcp-server-fetch` | — |
| git | `uvx mcp-server-git --repository /var/www/vera` | — |
| filesystem | `@modelcontextprotocol/server-filesystem /data` | — |
| memory | `@modelcontextprotocol/server-memory` | — |
| github | `@modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| instagram | `@pinkpixel/instagram-engagement-mcp` | IG token + business id |
| facebook | `@pinkpixel/facebook-pages-mcp` | Page token + page id |

Add via dashboard → MCP tab → preset. `vera-core` Dockerfile bundles `node 20`
and `uv` so both npm and Python MCP servers can be lazy-installed.

---

## 10. Adding a new source

1. Either: register an MCP server providing the data tools, or write a poller
   service like `vera-gmail/app/poller.py` that POSTs `/event`
2. Create a `Source` row via `/api/sources` with `type`, `filters[]`,
   `base_threshold`
3. Dashboard → Источники → edit filters. Done.

No code changes in vera-core needed — `Source` model + filter engine in
`shared/vera_shared/sources/filters.py` are source-agnostic.

---

## 11. Pending / open

- R2-R5: Decision-instead-of-menu triage; explicit-feedback calibration via
  Graphiti annotations; auto-execution at threshold; UI «Память» (graph
  browser)
- Self-extension (semi-autonomous MCP discovery + install with owner approval)
  — see separate proposal `docs/SELF_EXTENSION.md`
- Dedup `registration.py` / FastAPI boot into `shared/base_bot/`
- Performance: SQL filter for TokenPool, cache `collect_tools()`, drop
  `google-generativeai` SDK (already replaced by LiteLLM)
- M3 PII redaction in logs

---

## 11.5. Telegram context enrichment

Telegram poller decorates each event with:
- `folder`: dialog filter (folder) where the chat lives, e.g. «Работа»,
  «Личное». Cached 30min.
- `mutual_chats`: for private DMs, list of groups Vera shares with the
  sender. Cached 12h per-user via Telegram's `GetCommonChats`. Surfaces
  in `entity_hints` so Graphiti binds the person to their groups.

Filter predicates added: `folder`, `folder_in`, `folder_not_in`,
`mutual_chat_contains`. See `shared/vera_shared/sources/filters.py`.

## 11.6. Retrieval relevance gate

Graphiti `search()` returns top-N regardless of similarity. On a sparse
graph this surfaces unrelated episodes (the «veranda leak» — a message
from Marina was dragging in `domain veranda.my…namecheap` fact because
it was the only thing the graph had).

`triage/engine._is_relevant` post-filters retrievals:
- keep if any `entity_hint.identifier` appears in the fact, OR
- keep if persona/instruction/rejection signal present, OR
- keep if ≥10% of event content tokens (len≥3) overlap with fact tokens.

## 11.7. Research dump import

`POST /api/research/import` accepts `{source, documents: [{title, body,
url?, date?}]}` and enqueues each document as a Graphiti episode.
Dashboard «Память» tab provides a file upload widget.

Workflow for Perplexity Spaces backfill:
1. Install Chrome extension «Perplexity to Notion — Batch Export»
2. Export Space/Library → Markdown folder
3. Dashboard → Память → выбрать все .md → Загрузить в мозг

JSON exports (Perplexity API thread dumps, ChatGPT exports) are
auto-parsed if they look like `[{title, body, ...}]` or
`{threads: [...]}`.

## 12. Migration log (recent significant changes)

- 2026-05-21: Pack S/D/R/N — destructive-tools args resolved server-side
  for telegram_send_* too; AUTO_SAFE_TOOLS whitelist gates auto-mode;
  CSRF header on dashboard; deploy script gets rollback + image cleanup
  + Telegram failure DM
- 2026-05-21: Brain feedback loops (R3+R4) — decisions/rejections/
  instructions persist to Graphiti; hybrid retrieval with relevance
  gate; DM instructions inline-written
- 2026-05-21: Telegram folder + mutual_chats context, /api/research/import
- 2026-05-21: vera-git + vera-web removed (replaced by MCP fetch/git/github)
- 2026-05-21: Source model + filter engine + Telegram poller + dashboard editor
- 2026-05-21: Triage callback security (owner+chat check, server-side arg
  resolution, internal/agents require_owner, registration host allowlist)
- 2026-05-21: Custom-reply followup via reply-to-message or inline `#N`
- 2026-05-21: Gmail batch tools (`gmail_modify_threads`, `gmail_apply_label`)
- 2026-05-20: MCP foundation + dashboard CRUD + presets
- 2026-05-20: LiteLLM router replaces custom TokenPool for chat calls
- 2026-05-20: SSH-based GitHub Actions deploy (replaces self-deploy)

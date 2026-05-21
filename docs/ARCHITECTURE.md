# Vera 2.0 — Architecture

> Single source of truth for the project. Every future change must align
> with the principles and patterns here. If reality drifts from this doc,
> update the doc — don't accept the drift silently.

## Vision

Vera is an **AI orchestrator + brain** that reacts to triggers from many
sources (Telegram, Gmail, Instagram, bank, calendar, …) and proposes
actions in DM. Over time it learns the user's preferences and starts
acting autonomously on patterns the user has approved.

Hard rules:

- **Source-agnostic core.** Adding a new integration must not touch
  orchestrator code. Only adapters change.
- **MCP-first integrations.** If a Model Context Protocol server exists
  for a service, use it. Custom adapters only when no MCP exists.
- **Tools, not commands.** Every capability is exposed as a callable
  tool with a schema. The LLM picks tools; nothing hand-codes flows.
- **Per-trigger config.** For every source, user configures which
  trigger types fire, with optional custom prompt + auto-confidence.
- **Always-honest brain.** Vera reports what tools actually returned.
  Never fabricates names/numbers/dates not in tool output.
- **Decisions are training data.** Every user choice becomes context
  for the next similar event via RAG + distillation.

---

## Component map

```
                      ┌─────────────────────────────┐
                      │       vera-core              │
                      │  (FastAPI + agentic loop)    │
                      │                              │
                      │  ┌────────────────────────┐  │
                      │  │  Tool Registry         │  │
                      │  │  (MCP + HTTP + native) │  │
                      │  └────┬──────────┬────────┘  │
                      │       │          │           │
                      │  ┌────▼──┐   ┌──▼────────┐  │
                      │  │  MCP  │   │   HTTP     │  │
                      │  │client │   │   client   │  │
                      │  └────┬──┘   └──┬────────┘  │
                      │       │         │           │
                      │  Triggers / Watchers /      │
                      │  Triage / Card UX           │
                      └───┬───────────────┬─────────┘
                          │               │
       ┌──────────────────┼───────────────┼───────────────┐
       │  MCP servers     │               │  Custom HTTP  │
       │  (stdio / SSE)   │               │  adapters     │
       │  gmail / tg /    │               │  vera-bank /  │
       │  fetch / git /...│               │  vera-insta…  │
       └──────────────────┘               └───────────────┘

Memory (independent of source choice):
  Graphiti → Neo4j Aura (entities + episodes)
  SQLite   → events, decisions, triggers, gmail_accounts, tokens
```

Services in `docker-compose.yml`:

| Service       | Role                                        | Status                                 |
|---------------|---------------------------------------------|----------------------------------------|
| vera-core     | Orchestrator, dashboard, MCP manager        | always present                         |
| vera-telegram | Telethon userbot HTTP adapter               | being replaced by telegram-mcp (S3)    |
| vera-gmail    | Gmail OAuth + tools + poller                | being replaced by gmail-mcp (S2)       |
| vera-web      | Web search + fetch                          | being replaced by fetch-mcp (S4)       |
| vera-git      | Git + deploy tools                          | candidate for git-mcp (S4)             |

After S2-S4 most adapters disappear. Custom adapters remain only for
services without a usable MCP server.

---

## Layers (with frameworks)

| Layer            | What we use                              | Why                                                   |
|------------------|------------------------------------------|-------------------------------------------------------|
| HTTP server      | FastAPI + uvicorn                        | industry standard                                     |
| Telegram bot     | aiogram 3                                | best Python option                                    |
| Telethon userbot | Telethon                                 | only mature option                                    |
| Persistence      | SQLAlchemy 2 async + SQLite (WAL)        | enough for one user, ACID, zero ops                   |
| Knowledge graph  | Graphiti + Neo4j Aura Free               | bi-temporal entity memory done right                  |
| LLM access       | **LiteLLM router**                       | 200+ providers, retry, fallback, rate-limit, cost     |
| Integrations     | **MCP** (Anthropic protocol) where possible | huge community, zero code per integration             |
| Container        | Docker compose                           | fits one VPS                                          |
| Encryption       | cryptography (Fernet) + master in env    | tokens at rest                                        |

We deliberately did NOT take LangChain / LangGraph / Letta — too much
abstraction for our scale, debugging pain, vendor lock.

---

## Data model (SQLite)

```sql
-- API keys for LLM providers (Gemini / DeepSeek / Anthropic / Voyage)
tokens (id, provider, label, token (encrypted), capabilities JSON,
        is_active, daily_used, daily_limit, cooldown_until, error_count,
        last_used_at, tokens_in, tokens_out, cost_usd, …)

-- Registered tool sources (whether MCP server or HTTP adapter)
agents (id, name, http_url, capabilities JSON, tools JSON,
        status, last_heartbeat, …)

-- MCP server configurations
mcp_servers (id, name, transport, command JSON, env JSON,
             enabled, last_started_at, status, …)

-- User-defined event triggers, per source+account
triggers (id, source, account, name, predicate JSON,
          triage_prompt, auto_confidence, enabled, …)

-- Every event from any source, with triage result
events (id, source, source_event_id, account, category, content_text,
        content_extra JSON, entity_hints JSON, metadata JSON,
        occurred_at, triage_status, triage_result JSON,
        graphiti_episode_uuid, …)

-- Gmail OAuth (will move to mcp_servers.env once gmail-mcp wired)
gmail_accounts (id, email, refresh_token_enc, access_token_enc, …)
```

---

## Orchestration loop

`vera-core/app/orchestrator/loop.py`:

```
1. user input or event arrives
2. collect available tools from Tool Registry (MCP + HTTP + native)
3. LLM (via LiteLLM router): "given task + tools, pick next action or finish"
4. if pick tool: route to MCP client OR HTTP adapter; result back to LLM
5. repeat up to MAX_ITERATIONS
6. final answer to user (with trace as separate message)
```

LLM input contract:
- system prompt declares tools, principles, anti-hallucination rules
- conversation memory (last N turns) injected
- per-trigger triage prompt injected when applicable (from triggers table)

---

## Trigger architecture

User configures per source+account which events fire Vera:

```
predicate examples:
  Gmail   : {"from_contains": "boss@"} or {"has_label": "Important"}
  Telegram: {"from_user": "@andrey"} or {"mentions_me": true}
  Bank    : {"amount_gt": 10_000_000}
```

For each event the adapter/watcher evaluates the predicate. If match:

1. Build `Event` with `trigger_id` + `trigger.triage_prompt`
2. Post to `/event`
3. Triage uses LLM with merged system prompt (default + trigger prompt)
4. Card with proposed actions to user
5. User picks → callback executes the chosen tool (S5)
6. Decision recorded → memory + future triage

Auto-mode: if `trigger.auto_confidence > 0` and proposed action confidence
exceeds it, Vera executes silently and adds to daily digest instead of
asking.

---

## How to extend

### Add a new MCP server (preferred)

1. Find MCP server (https://github.com/modelcontextprotocol/servers etc.)
2. Add row in `mcp_servers` via dashboard or:
   ```sql
   INSERT INTO mcp_servers (name, transport, command, env, enabled)
   VALUES ('gdrive', 'stdio',
           '["npx", "@modelcontextprotocol/server-gdrive"]',
           '{"GDRIVE_TOKEN": "…"}', 1);
   ```
3. vera-core auto-connects on next restart, discovers tools.
4. Tools are immediately available to Vera.

### Add a custom HTTP adapter (when no MCP exists)

1. New directory `vera-XXX/` with same layout as `vera-bank/`.
2. Implement `tool_specs.py` (declare tools) + `tool_handlers.py` (handlers).
3. Add to `docker-compose.yml`.
4. Adapter registers itself with vera-core on startup (X-Internal-Secret).

### Add a new trigger type for an existing source

1. Add predicate handler in `vera-core/app/triggers/predicates.py`.
2. Add UI option in `dashboard/index.html` triggers form.
3. No core changes.

### Add a custom slot to user persona

1. Edit `vera-core/app/persona/distillation.py` system prompt — add a new
   facet to extract.
2. Migration: existing `persona_doc` will re-distill at next periodic run.

---

## Security invariants

- Tokens at rest are encrypted with Fernet using `SESSION_SECRET`.
- Internal HTTP between containers requires `X-Internal-Secret` header.
- Dashboard requires Telegram Login Widget signed payload, `id` must
  equal `OWNER_TELEGRAM_ID`.
- Deploy webhook requires `Authorization: Bearer <DEPLOY_SECRET>`.
- Adapters register only with `http_url` matching `http://vera-*` —
  external URLs rejected.
- Bot ignores all instructions found inside tool results / message
  bodies (prompt injection defense in loop.py system prompt).

---

## Testing

`vera-core/tests/` uses pytest + pytest-asyncio.

- Unit tests close to the code they test. No mocking of LiteLLM for now —
  we run with a real cheap model (gemini-flash-lite) in CI.
- Integration tests in `tests/integration/` are opt-in (require live
  Neo4j Aura + tokens), tagged `@pytest.mark.integration`.
- Run locally: `pytest vera-core/tests`.
- CI runs on every push.

---

## Migration log

Each major architectural change should be logged here so future
contributors understand WHY decisions look the way they do.

- **2026-05-21**: token pool + custom provider clients replaced with
  LiteLLM router. Migration kept fallback chain semantics (Gemini → DeepSeek
  → Anthropic) and per-key cost tracking. Old `providers/registry.py`
  remains as thin compatibility shim until all callers are migrated.
- **2026-05-21**: scaffolding for MCP integrations. `mcp_servers` table
  added. Manager not yet wired to live connections — S1 will activate.
- **2026-05-19** (earlier): switched memory to Graphiti+Neo4j Aura after
  custom SQLite-only approach didn't scale across sources.

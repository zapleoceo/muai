# Brain

The "intelligence layer" — three sub-services that turn events into useful answers.

## brain-triage

`services/brain-triage/src/brain_triage/worker.py`

- Loop: every 5s claim a batch of `pending` events via `UPDATE … FOR UPDATE SKIP LOCKED RETURNING`.
- For each event: build a structured prompt → call AIbroker `chat:fast` with `response_format=json_object` → parse → write to `events.triage_metadata` (importance, topics, people, signals, needs_action).
- Voyage embedding in the same loop, batched (one call per N events).
- Concurrency: `TRIAGE_CONCURRENCY=5` events processed in parallel per worker.
- Scale: replicas. `docker compose up -d --scale brain-triage=3` triples throughput safely (SKIP LOCKED guards).

## brain-search

`services/brain-search/src/brain_search/app.py`

- `POST /search` — entry point for the Telegram bot and dashboard.
- Hybrid retrieval: FTS (`to_tsvector('russian')` + ts_rank) AND cosine similarity over Voyage embeddings.
- ReAct agent loop (`agent.py`):
  - LLM emits strict JSON each step: `{action: 'tool', name, params}` or `{action: 'answer', text}`.
  - Tools available: `search_events`, `memory.remember`, plus everything from `ingestor-telegram` via `/tools/spec` HTTP discovery.
  - Max 6 steps. Returns AnswerResponse with provider, cost, agent_trace.

## bot-telegram

`services/bot-telegram/src/bot_telegram/bot.py`

- aiogram polling (no webhooks).
- Owner-only — every message checked against `OWNER_TELEGRAM_ID`.
- Persists user query AND Vera reply to `events` with `source='vera_chat'` — that's how conversation history survives bot restarts.
- Calls `brain-search /search` with `conversation: {chat_id}` so search itself pulls last N pairs as context.

## Identity / memory

- `entities` + `entity_aliases` + `memberships` — substrate for L1 graph (people, groups, chats).
- `identity_nodes` (type='style'|'fact'|...) — L3 identity layer (Vera's persona / style profile, learned facts).
- `patterns` — L2 reserved for the future (recurring trigger→action with weight).
- See [domain-model.md](./domain-model.md) for full schema.

## Conventions

- All LLM calls go through `vera_shared.llm.client.chat()` / `embed()`.
- `workflow=` kwarg is REQUIRED — it's how we group calls in `usage_log`.
- Capability is one of: `chat:fast`, `chat:smart`, `chat:code`, `prefilter`, `structured`, `vision`, `embedding`.
- Cost guard is at the broker — don't duplicate in callers.

# Brain

The "intelligence layer" — three sub-services that turn events into useful answers.

## brain-triage

`services/brain-triage/src/brain_triage/worker.py`

- Loop: every 5s claim a batch of `pending` events via `UPDATE … FOR UPDATE SKIP LOCKED RETURNING`.
- For each event: build a structured prompt → call AIbroker `chat:fast` with `response_format=json_object` → parse → write to `events.triage_metadata` (importance, topics, people, signals, needs_action).
- Voyage embedding in the same loop, batched (one call per N events).
- Concurrency: `TRIAGE_CONCURRENCY=5` events processed in parallel per worker.
- Scale: replicas. `docker compose up -d --scale brain-triage=3` triples throughput safely (SKIP LOCKED guards).

## Backfill pause + rate limit

Two controls on the 📥 Live прогресс dashboard card, both stored in the
`app_control` KV table (`vera_shared.control`, migration 009), so they
hold across restarts/deploys:

- **⏸ Пауза / ▶ Продолжить** — flips `backfill_paused`. Both
  `brain-triage` `process_pending()` and `media-worker`'s loop check
  `is_backfill_paused()` at the top of each cycle and skip claiming while
  paused. Events stay `pending` / `media_pending` and resume in place.
- **Лимит запросов/час** — `backfill_max_per_hour` (0 = unlimited).
  Even-tempo throttle: the hourly cap is spread to a per-minute budget
  (`backfill_minute_allowance()`), and each worker claims at most that
  many items per cycle, so the request rate stays flat instead of
  bursting and burning the providers' free-tier quota. The budget is
  global across triage + media + replicas — measured from `usage_log`
  (`workflow IN triage/media_vision/media_voice` in the trailing 60 s).
  Live events share the same budget (they also write `workflow=triage`),
  so the cap bounds total throughput, leaving headroom for new messages.

Live ingest (Telegram/Gmail/IG) is never throttled — only LLM-consuming
processing is paused/paced.

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

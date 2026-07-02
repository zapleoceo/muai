# Brain

The "intelligence layer" — three sub-services that turn events into useful answers.

## brain-triage

`services/brain-triage/src/brain_triage/worker.py`

- Loop: every 5s claim a batch of `pending` events via `UPDATE … FOR UPDATE SKIP LOCKED RETURNING`.
- For each event: build a structured prompt → call AIbroker `chat:fast` with `response_format=TRIAGE_JSON_SCHEMA` (json_schema, strict=True — see below) → parse → write to `events.triage_metadata` (importance, topics, people, signals, needs_action).
- Voyage embedding in the same loop, batched (one call per N events).
- Concurrency: `TRIAGE_CONCURRENCY=10` events in parallel per worker
  (was 5; bumped 2026-07-01 for backfill drainage — Mistral latency
  ~1.1s, 0.05% error rate leaves plenty of headroom).
- Scale: replicas. Default `BRAIN_TRIAGE_REPLICAS=5` in compose (was 3);
  `docker compose up -d --scale brain-triage=N` still works. `SELECT FOR
  UPDATE SKIP LOCKED` guarantees no two workers claim the same event.
- Combined ceiling: 5 replicas × 10 concurrency = 50 in-flight LLM
  calls at any time; practical throughput bounded by broker rate limits
  (~10-14k triage/hour on the current key pool).
- If you need to walk it down (broker/Postgres pressure): edit the
  defaults in `vera3/infra/docker-compose.yml` or override in server
  `.env` (`TRIAGE_CONCURRENCY=…`, `BRAIN_TRIAGE_REPLICAS=…`) + restart.

## Structured output: json_schema, not json_object

2026-07-02: both `worker.py::triage_one` (workflow=`triage`) and
`vera_shared/graph/rel_extract.py::extract_and_store` (workflow=`rel_extract`,
~214k calls/week — the largest structured-traffic source) switched from
`response_format={"type": "json_object"}` to a full `json_schema` with
`strict: true`:

```python
{
  "type": "json_schema",
  "json_schema": {
    "name": "triage",           # or "rel_extract"
    "strict": True,
    "schema": {"type": "object", "properties": {...},
               "required": [...], "additionalProperties": False},
  },
}
```

Why: `json_object` just tells the model "output JSON" — the model still
picks its own shape, and providers without careful prompting (cerebras
gpt-oss was the worst offender) sometimes emit malformed JSON that
`json.loads()` can't parse. `json_schema` with `strict: true` triggers
**grammar-constrained decoding** on providers that support it (gemini,
openai-compatible, groq) — the model is *physically* prevented from
emitting a token that violates the schema (wrong enum value, missing
required key, extra property). AIbroker forwards `response_format`
verbatim to LiteLLM (`routes/proxy.py` → `litellm_adapter.py`); it does
no schema validation/transformation itself, so the schema Vera sends is
exactly what reaches the provider.

Constants: `brain_triage.worker.TRIAGE_JSON_SCHEMA`,
`vera_shared.graph.rel_extract.REL_EXTRACT_JSON_SCHEMA`. Both are built
from the same enum sources the code already validates against
(`PROJECT_VOCAB`, `PREDICATES`) so the schema and the client-side
`postprocess_triage()` / predicate check can't silently drift apart —
see `vera3/tests/unit/test_triage_json_schema.py` and
`test_rel_extract_schema.py` for the drift guards.

`postprocess_triage()` is **not** removed even though the schema now
constrains generation — providers where LiteLLM's `drop_params` silently
strips an unsupported `response_format` still need the client-side
defense-in-depth.

Providers ignoring `strict` json_schema (or not supporting it) just fall
back to a normal completion guided by the prompt's `"Верни СТРОГО JSON
по схеме"` instruction — same behavior as before, no regression.

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

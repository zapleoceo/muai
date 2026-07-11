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

> **Prompt compressed ~30% (2026-07-08), token quota is the real constraint,
> not $ cost.** Confirmed live via Cerebras' own API: `usage.prompt_tokens`
> for a repeated-prefix request is IDENTICAL whether the prefix hits their
> server-side cache or not (`cached_tokens=640/750` vs `0/750`, same total).
> Caching is their internal speed optimization — it does **not** discount
> against our daily token quota (`x-ratelimit-*-tokens-day`). The static
> instructional part of `TRIAGE_PROMPT_TEMPLATE`/`TRIAGE_BATCH_PROMPT_HEADER`
> (role, Dima's context, JSON shape spelled out, project/nature/ready_subtype
> rules) was ~750-950 tokens repeated on every single call — at ~16-17k
> triage calls/day, enough alone to saturate cerebras' whole 14-key daily
> budget with zero real message content. Compressed the wording (verbose
> multi-line JSON block → one-line shape; 2-3 examples per ready_subtype case
> → one each) and extracted the duplicated context/rules text (previously two
> independently-hand-written copies) into shared `_TRIAGE_CONTEXT`/
> `_TRIAGE_RULES`/`_TRIAGE_TOPICS` constants — one source to keep in sync
> going forward. All field names/enum values/semantics are unchanged;
> `TRIAGE_JSON_SCHEMA`/`TRIAGE_BATCH_JSON_SCHEMA` (the enforced structure for
> schema-capable providers) and `postprocess_triage()`'s validation/
> normalization are untouched — this only compresses the human-readable
> instructions. Confirmed live on Cerebras (`gpt-oss-120b`, real API, same
> test content): **898 → 628 prompt tokens (-30%)**, and the compressed
> prompt still produces correct, complete JSON (`project`, `needs_action`,
> `people_mentioned` all verified against a known-answer test message).
> `tests/unit/test_triage_group_batch.py` (35 tests) still passes unchanged.

> **`ix_events_pending_claim` (migration 014, 2026-07-11) — the claim query
> needs a partial index for `triage_status='pending'`, same as the existing
> ones for `'processing'`/`'error'`.** Without it, once the backlog empties,
> `_claim_batch()`'s `WHERE triage_status='pending' ORDER BY occurred_at DESC
> ... FOR UPDATE SKIP LOCKED` has no way to skip non-pending rows — the
> planner walks `ix_events_occurred_at` backwards, filtering row-by-row,
> effectively scanning the whole table on every single poll. Measured on
> production with ~403k rows: 2.9s/call, 387k buffer touches, 0 rows found —
> ×5 replicas ×every 5s ≈ sustained ~100% CPU on a 2-core box (load average
> 3.4+) confirming there's nothing to do. After the index: same query,
> ~0.05ms, 1 buffer touch. Applied `CONCURRENTLY` directly on production
> (see migration file for why); load average dropped to <1 within a minute.

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

## Group message batching

2026-07-02: `backfill_max_per_hour` rate-limits LLM **calls**, not
events (see below). The one lever that increases effective throughput
without touching that cap is packing more events into each call.

Telegram group chats (`metadata.chat_kind == "group"` — supergroups +
legacy small `Chat`) carry short messages (median ~260 chars in the
current backlog). `process_pending()` batches up to
`TRIAGE_GROUP_BATCH_SIZE` (default 10) of them into **one** `chat()`
call using `TRIAGE_BATCH_JSON_SCHEMA` (an array of per-event results,
each tagged `event_id` so the response maps back correctly — LLM
response order isn't guaranteed). `_chunk_group_rows()` also caps total
batch text at `TRIAGE_GROUP_BATCH_MAX_CHARS` (default 6000) so one
outlier-long message can't blow up a batch's context.

**Channels** (`chat_kind == "channel"`, broadcast, longer posts —
median ~370 chars, p99 ~2200) and **private chats**
(`chat_kind == "private"`) are **not** batched — each event still gets
its own `triage_one()` call, same as before this feature. Rationale:
mixing unrelated broadcast posts in one call dilutes context quality;
private messages (mostly Dima's own conversations) get full per-message
model attention deliberately.

Batching changes nothing about **what gets stored**: `events.metadata`
(author, chat_id, direction, sender — set at ingest by `userbot.py`) is
never touched by triage, batched or not. Only the *triage call grouping*
changes — `events.triage_metadata` is still written per-event from the
batch response, same shape as the single-event path.

**Partial-response handling**: if the LLM returns fewer `results` than
events sent (truncation, refusal on one item), the missing event_ids get
`None` → `triage_group_batch()` returns `{event_id: None}` for them →
the caller sets those back to `pending` for an individual retry on the
next cycle. Nothing is silently dropped. A hallucinated `event_id` not
in the request is ignored rather than corrupting an unrelated event.

`rel_extract` is **not** batched (fires per-event, fire-and-forget,
unaffected either way).

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
  - Each step is wrapped in `asyncio.wait_for(..., timeout=AGENT_STEP_TIMEOUT_S)`
    (default 90s, env-overridable) — a hung broker call used to block
    `/search` indefinitely; now it returns "LLM не ответил вовремя" instead.

### Monthly reports (`reports.py`) — exact aggregation, no LLM

For requests like "отчёт заказов помесячно за 2026 год": summing numbers
across a whole year is the wrong job for retrieval-then-LLM-synthesize —
even a widened limit truncates most months, and an LLM shouldn't be
trusted to add up hundreds of numbers correctly anyway. This path
intercepts the request in `search()` *before* the embed call and answers
straight from SQL, `cost_usd=0.0`, `provider="vera-report"`:

1. `detect_report_request(q)` — trigger words ("отчёт", "помесячно", "по
   месяцам", "статистик") + optional year.
2. `find_report_chat(q)` — matches a `project_membership.label` (exact
   chat title) as a substring of the query. No match → falls through to
   normal retrieval; we never guess which chat "заказы" means.
3. `build_monthly_report(chat_id, chat_title, year)` — pulls every event
   for that chat/year (no LIMIT — the whole point is a complete sum),
   parses `key: value` lines appearing after a `---` body separator, and
   groups by month. Fields are split into two kinds: flow fields (`b/n`,
   `contract, SC`, `expenses` — summed per month) and snapshot fields
   (`ost`, `Lead Point` — a running balance/score, so the month's value
   is the *last* one seen, never a sum of snapshots).
4. `detect_target_field(q)` — a business-term → field-name map
   (`FIELD_INTENT_MAP`, e.g. "заказ" → `b/n`, set per Dima's own
   definition for the "Jakarta: sms report" chat) controls the output
   shape: `render_simple_markdown()` (compact "месяц — сумма" for one
   field) by default, or `render_report_markdown()` (every field) when
   the query says "детально"/"подробно"/"все поля".

Chat message formats are not guaranteed stable over time — messages that
don't parse as `key: value` are counted as "unstructured" and excluded
from sums rather than silently treated as zero.

## bot-telegram

`services/bot-telegram/src/bot_telegram/bot.py`

- aiogram polling (no webhooks).
- Owner-only — every message checked against `OWNER_TELEGRAM_ID`.
- Persists user query AND Vera reply to `events` with `source='vera_chat'` — that's how conversation history survives bot restarts.
- Calls `brain-search /search` with `conversation: {chat_id}` so search itself pulls last N pairs as context.
- `bot_telegram/formatting.py` — `format_reply()` HTML-escapes the LLM
  answer before sending with `parse_mode=HTML` (unescaped `<no-reply@...>`
  style addresses in an answer used to break Telegram's HTML parser and
  silently drop the reply). `plain_fallback()` is the second line of
  defense: if Telegram still rejects the HTML (`TelegramBadRequest`),
  resend as plain text rather than lose the answer. `format_error()`
  formats the user-facing error message on any other failure.

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

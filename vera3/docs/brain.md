# Brain

The "intelligence layer" — three sub-services that turn events into useful answers.

## brain-triage

`services/brain-triage/src/brain_triage/worker.py`

- Loop: every 5s claim a batch of `pending` events via `UPDATE … FOR UPDATE SKIP LOCKED RETURNING`.
- For each event: build a structured prompt → call AIbroker via `resolve_triage_capability()` (prefers `chat:fast`; falls back to `chat:smart` when `chat:fast`'s free daily pool is budget-capped — a same-cost free tier, so the queue drains 24/7 instead of stalling ~5h/day until the 00:00 UTC reset) with `response_format=TRIAGE_JSON_SCHEMA` (json_schema, strict=True — see below) → parse → write to `events.triage_metadata` (importance, topics, people, signals, needs_action). The worker only idles when **both** capabilities are capped.
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

Constants: `brain_triage.schemas.TRIAGE_JSON_SCHEMA`,
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
the caller sets those back to `pending` with
`triage_error = BATCH_MISS_ERROR` (concurrency.py). On the next claim
such events are **excluded from group batching** and retried singly —
otherwise the batch path could omit the same event forever
(pending → batched → omitted → pending …). Nothing is silently dropped.
A hallucinated `event_id` not in the request is ignored rather than
corrupting an unrelated event.

**Retry backoff (two-phase, 2026-07-17)**: `_retry_failed_loop()` first
*schedules* a fresh `error` event (`triage_next_retry_at = NOW() +
BACKOFF_MINUTES[retry_count]`, or straight to `dead` after
`MAX_RETRIES`), then *releases* ripened ones back to `pending` and bumps
the counter. The old single-UPDATE re-pended the first failure instantly
and indexed the array off by one (1m step unused, ladder shifted).

**Contentless events are not embedded (2026-10)**: `is_contentless(text,
source)` (`vera_shared/text_quality.py`) applies only to the chat sources
`telegram` / `slack` / `instagram` and only to text in their layout: header
lines (`Author: ...`), the FIRST `---` separator, then the body
(`split_header_body()` returns `None` for any other layout, so a markdown
`---` inside a body is never a boundary). Deliberate records (`claude`,
`vera_memory`, `voice`, `claude_chat`, gmail, text without that header) are
never skipped: "Купил хлеб" from `claude` is embedded, `[photo]` from
telegram is not. Placeholder tokens (`[photo]`, `[voice]`, `[sticker]`) are
stripped and a body remainder shorter than `MIN_CONTENT_CHARS` (11) is
contentless. `process_pending()` skips the embedding but keeps the event.
Existing vectors are not deleted.

**Re-embed loop (2026-10)**: `reembed_loop()` (`reembed.py`, started from
`start_background_loops()` as `triage-reembed`) runs every
`TRIAGE_REEMBED_INTERVAL_S` (600). Each pass is two id-range-bounded windows
(plan verified on prod: bitmap scan of `events_pkey` over the range +
anti-join on the `event_embeddings` PK, no walk of the whole table): the
*head* — the last `HEAD_SPAN` ids, read whole so placeholder rows that never
get a vector cannot crowd real gaps out, so a fresh gap closes in one cycle —
and the *tail* — a `TAIL_SPAN` window below a descending cursor that slides
down every cycle and resets at the bottom, picking up old gaps slowly. Up to
`TRIAGE_REEMBED_BATCH` (50) per window are embedded via `write_embeddings()`
(`embeddings.py`, the same upsert path as triage, chunks included), skipping
`SKIP_EMBED_SOURCES` and contentless bodies. `reembed_once()` does nothing
while the `embed` circuit (including the broker-outage breaker) is open.

**Stale-worker fencing (2026-07-17)**: `_claim_batch()` returns
`triage_started_at` and every final UPDATE in `process_pending()`
matches on it. If the watchdog re-pended an event mid-run (processing
exceeded `STUCK_AFTER_S`) and another replica re-claimed it, the slow
worker's stale result matches 0 rows and is discarded (logged as
"fenced out") instead of overwriting the fresher state; its rel-extract
is skipped too.

`rel_extract` is **not** batched (fires per-event, fire-and-forget,
unaffected either way).

### rel-extract admission threshold

Two gates decide whether a triaged event gets a relationship-extraction
call at all — it is the most expensive background work the worker does
(one `structured` LLM call plus up to ~10 DB sessions resolving entity
names, all outside `TRIAGE_CONCURRENCY`):

1. `importance >= REL_EXTRACT_MIN_IMPORTANCE` (`brain_triage/config.py`,
   env `TRIAGE_REL_MIN_IMPORTANCE`, default **60**).
2. `rel_extract_skip_reason(source, metadata, content_text)`
   (`vera_shared/graph/rel_policy.py`, bool form `should_extract_relations`)
   — no LLM, decided from the event itself. The worker logs one INFO line
   per batch: `rel-extract: в работу N, отсеяно гейтом {reason: count}`.
   `scripts/reindex_rels.py` goes through the same gate.

#### Gate by data, not only by importance (2026-09-13)

Measured on prod: rel-extract made ~855 LLM calls a week and produced
**zero** relationships since 2026-09-04. On 12 real importance>=60 events
every model (gemini-3.5-flash-lite, gpt-oss-120b) honestly answered `[]` —
channel news, bot notifications, announcements, chatter without any
relationship in it; on control sentences with explicit relationships the
same models extract 2-3 correctly. The calls were burned where no
relationship could exist.

Skip reasons, in order. Measured on the 30-day candidate pool
(importance>=60, 8963 events); in brackets — how many of those events had
ever produced a relationship:

| reason | what | events (had rels) |
|---|---|---|
| `source` | `claude_chat`, `vera_chat` — transcripts with assistants about code | 3778 (10, all junk: «Дима works_at GitHub») |
| `channel` | broadcast-channel post | 450 (0) |
| `no_participation` | group where the owner never writes (`owner_participates`) | 149 (0) |
| `machine_sender` | `is_machine_sender`: JIRA/calendar/newsletter mail («(JIRA)» in the name, `jira@`, service mailbox per `identity.entity_kind_for_email`), Telegram `...bot` usernames | 1304 (19: «Ольга Крячко (JIRA) works_at Artem Belov») |
| `short` | message body (ingestor header cut by `ingest/envelope.message_body`) under 30 chars | 721 (3) |
| `no_marker` | `has_relation_marker`: not a single relationship word (ru/uk/en/id stems: работает, начальник, жена, колега, works at, wife, istri...) in the body | 2068 (30) |

What passes: **493 of 8963 (5.5%)** — telegram 340, voice 51, slack 50,
gmail 45, vera_memory 6. The header is excluded from the marker search on
purpose: the chat title «Веранда сотрудники» made every member a
«сотрудник». The events dropped by the gate that had produced edges were
the same junk classes the validator below rejects.

Rejected ideas: counting capitalised words as «two named entities» — news
(«Киев», «БПЛА», «Иран») pass it just as well; an LLM pre-classifier —
that is the very call we are trying to save.

#### Validation before writing and run counters

`extract_and_store` returns `RelExtractOutcome` (`returned`, `unresolved`,
`rejected` by reason, `inserted`, `llm_failed`) and logs it at INFO for
every run — a zero output is visible, not buried in DEBUG. LLM failure and
an off-schema answer are WARNING (the raw answer is not logged: it carries
facts from private correspondence). Each resolved tuple goes through
`relationship_reject_reason` before `upsert_relationship` — see
`graph-dedup.md`.

**The importance scale is 0-100**, defined in `schemas.py` and restated in
`prompts.py`. The threshold used to be a hardcoded `3`, which admits
essentially the whole stream while the comment beside it promised "only
high-signal events" — it reads like it was written against a 1-5 scale.
At 60 the gate means what it says: rel-extract fires on events the model
rated clearly above routine, not on every "ок, договорились".

Set `TRIAGE_REL_MIN_IMPORTANCE=0` to restore the old build-the-graph-from-
everything behaviour.

### Эмбеддинги: pgvector и смысловой поиск (миграция 030)

`VERA.md` и `architecture.md` с самого начала обещали «Postgres + pgvector
для эмбеддингов», и образ базы действительно `pgvector/pgvector:pg16` (на
проде расширение 0.8.2) — но колонка была `JSONB`, ANN-индекса не было, а
косинус считался циклом на Python в двух местах: `brain_search/scoring.py`
и `gateway/claude.py` (до **500** строк на каждый `/v1/claude/remember`).

**Главное следствие — поиск «по смыслу» фактически не работал** (замер
2026-09-13: 444 тыс. эмбеддингов voyage-4 × 1024, 3.6 ГБ, 66% базы).
brain-search отбирал ≤200 кандидатов полнотекстом `to_tsvector('russian')`,
по времени или по проекту и только потом считал косинус. Текст, не делящий
с вопросом ни одного слова («аренда» → «invoice for the villa»), до косинуса
не доезжал никогда.

#### Как устроено теперь

- **Колонка** `event_embeddings.embedding_vec halfvec(1024)`, `STORAGE
  PLAIN` (миграция 030, исправлена до наката: первая редакция объявляла
  `vector(1024)`).
- **Индекс** `ix_event_embeddings_vec_bq` — HNSW по бинарному квантованию
  `(binary_quantize(embedding_vec)::bit(1024)) bit_hamming_ops`
  (`vectors.ann_index_sql`), строится `backfill_pgvector.py --index`
  `CONCURRENTLY`.
- **Отбор** (`vectors.ann_candidates_sql`): индекс по Хэммингу отдаёт
  `ANN_OVERSAMPLE` = 1000 грубых кандидатов, они пересчитываются точным
  косинусом по halfvec, наверх уходят 200 (`ANN_POOL`). `hnsw.ef_search`
  ставится равным 1000 (по умолчанию 40 — HNSW молча вернул бы 40 строк), а
  `hnsw.iterative_scan = relaxed_order` (pgvector ≥0.8) продолжает скан, пока
  фильтр по проекту/времени не наберёт LIMIT (`ANN_SETTINGS_SQL`,
  `ann_settings_params`). Выражение `bq` в запросе совпадает с выражением
  индекса дословно — иначе планировщик индекс не узнает, и тест это держит.
- **Гибрид** (`brain_search/ann.py`, `retrieval.fetch_candidates`): основной
  режим (`project`/`fts`/`time`/`vector`/`recent`) работает как раньше, а
  смысловые кандидаты с тем же фильтром (`semantic_filter` — проект, окно,
  «не разговор с Верой», но без текстового условия) добавляются сверху
  (`fetch_ann_rows`, `merge_candidates` — только новые id). Форму строк
  задают `ann_rows_sql`/`vec_sim_column`, параметры — `ann_params`, `q_cast`.
- **Скоринг** (`scoring.row_similarity`) берёт косинус из колонки `vec_sim`,
  посчитанной Postgres; у строки без него 0. Седьмая колонка кандидата
  (`embedding`) зарезервирована и всегда NULL: вектор не покидает БД. `vec_sim`
  стоит ПОСЛЕДНЕЙ колонкой: rank и account scoring читает по позициям 7 и 8.
- **Без проверок «колонка/индекс есть»** (пробы наличия колонки и ANN-индекса
  сняты вместе с JSONB): ANN идёт всегда, когда есть
  вектор запроса. Снесённый индекс без рестарта по-прежнему не роняет поиск:
  смысловой шаг в точке сохранения с `statement_timeout` 5 с.
- **Таблица заблокирована** (`VACUUM FULL event_embeddings`): основной запрос
  с JOIN по ней идёт под `lock_timeout` 3 с (`vectors.LOCK_TIMEOUT_MS`, параметры — `lock_timeout_params`,
  `retrieval._primary_with_degrade`); по таймауту поиск повторяется без вектора
  и без ANN, то есть на полнотексте. Без вектора запроса `event_embeddings`
  в запросе не участвует вовсе.
- **Дедуп `/v1/claude/remember`**: один запрос, косинус по halfvec в SQL
  (`ORDER BY <=> LIMIT 1`), питоновского перебора нет. Индекс не нужен:
  claude-событий за 7 дней десятки. Вектор запроса кастуется в `halfvec` без
  размерности — pgvector сверяет её с колонкой при сравнении (зашитая
  `halfvec(1024)` 13.09.2026 уронила CI, где колонка `halfvec(3)`). Запись —
  одна функция `vectors.embedding_upsert(event_id, vec)` для триажа, reembed и
  remember: пишет только `embedding_vec`.
- **Снятие JSONB** (миграции 038/039, runbook — deploy-ops.md «Снятие
  JSONB-эмбеддингов»): `embedding` JSONB (~6.3 КБ на строку, ~3.5 ГБ TOAST)
  больше не читается и не пишется. Строки без вектора (2484, все JSON `null`)
  удаляются 039, события остаются, цикл reembed в brain-triage эмбеддит их
  заново. Юнит-тесты на SQLite колонку `embedding_vec` не видят (ORM её не
  объявляет): SQL с векторами проверяется в `tests/integration` на Postgres
  с pgvector, где `reset_schema` добавляет `halfvec(3)`.

#### Почему halfvec + HNSW по битам, а не HNSW по вектору или IVFFlat

Ресурсы прода (2026-09-13): хост 7.7 ГБ RAM, доступно ~0.85 ГБ, swap занят
на 2.6 ГБ; у `vera3-postgres` `mem_limit: 768m`, `shared_buffers` 128 МБ,
`maintenance_work_mem` 64 МБ, `shm_size: 256mb`; 4 vCPU; диск свободно 34 ГБ.

| вариант (444 тыс. × 1024) | данные | индекс | память на постройку | запрос |
|---|---|---|---|---|
| `vector` + HNSW (m=16) | ~1.8 ГБ | ~1.9 ГБ | граф ~2 ГБ ≫ 768m → постройка на диске, часы | индекс в 2.5 раза больше лимита контейнера: каждый запрос — сотни случайных чтений холодного индекса |
| `halfvec` + HNSW | ~0.9 ГБ | ~1.0 ГБ | граф ~1.1 ГБ > 768m → тоже на диске | индекс всё ещё больше лимита |
| `halfvec` + IVFFlat (lists≈670) | ~0.9 ГБ | ~0.9 ГБ | мало | probes=10 → ~6.6 тыс. векторов на запрос; recall падает по мере дрейфа центроидов, нужны пересборки |
| **`halfvec` + HNSW по `bit(1024)` + пересчёт** | ~0.9 ГБ | **~0.2 ГБ** (128 байт вектор + соседи) | **~0.2 ГБ, влезает в 192 МБ** | индекс помещается в память; пересчёт 1000 строк по 2 КБ ≈ 2 МБ чтения |

Цена битового индекса — точность грубого шага, её возвращает запас
кандидатов. Замер recall@10 (биты → пересчёт против точного перебора) на
случайной выборке прода, 60 запросов: корпус 1 тыс. — 50 кандидатов 0.995,
100 — 1.0; корпус 3.7 тыс. — 50: 0.965, 100: 0.992, 200: 1.0. Запас растёт с
корпусом, поэтому взят потолок `ef_search` — 1000; фактический recall на
всём корпусе — `backfill_pgvector.py --recall` (порог приёмки в
deploy-ops.md). Ошибка косинуса float16 на той же выборке — 4e-5.
`mem_limit` postgres НЕ поднимался: на хосте нет свободной памяти, и
выбранная схема в текущий лимит укладывается.

Почему `STORAGE PLAIN`: у типов pgvector хранение EXTERNAL, строка halfvec
(2 КБ + заголовок) переваливает порог TOAST ~2 КБ и уехала бы в toast —
тогда пересчёт 1000 кандидатов стоил бы ещё ~2000 чтений toast-индекса и
чанков.

#### Обслуживание индекса

`scripts/backfill_pgvector.py` (имя от переливки JSONB → halfvec, которой
больше нет): `--status` (строки, индекс), `--index` (`build_index`: сносит
невалидный остаток прерванной постройки, `index_build_statements` —
`maintenance_work_mem` 192 МБ, без параллельных воркеров, т.к. их граф живёт
в `/dev/shm` 256 МБ), `--recall N` (`recall`, `recall_at_k`). Порядок наката и
отката — `deploy-ops.md`, «pgvector: накат и откат».

### Длинные тексты: потолок входа, отрывок для LLM, куски (миграция 032)

**Что было.** gmail (`_extract_text(...)[:8000]`), voice и claude_chat
(`body_text(...)[:8000]`) резали текст на входе. Замер 2026-09-13 за 30 дней:
163 письма и 95 сессий Claude упёрлись в потолок, их хвост **не хранится
нигде** — ни в `content_text`, ни в полнотексте. Длинное событие представлял
один вектор по первым 8000 символам: абзац про аренду в письме на 8 тыс.
тонул в «похоже на всё письмо».

**Одна политика — `vera_shared/text_chunks.py`:**

- `clip_content` — потолок `content_text` `MAX_CONTENT_CHARS` = 32 000 (было
  8000). Используют ингестор gmail, `gateway/voice.py`, `gateway/claude_session.py`.
- `llm_excerpt` — что видит LLM триажа и вектор события: ≤8000 символов,
  ¾ головы + ¼ хвоста (в письме подпись и последняя реплика ветки — в конце).
  Вызов триажа не раздувается. Групповой батч (`prompts.py`, `[:2000]`) не
  менялся — затрагивает 0.29% событий.
- `split_chunks` — куски по 1500 символов с перекрытием 200, разрез по
  абзацу → строке → концу предложения → пробелу в правой половине окна,
  начало следующего куска — с границы слова; не больше `MAX_CHUNKS` = 24
  (покрывает 32 тыс.). `needs_chunks` — длиннее `CHUNK_THRESHOLD` = 4000.

**Куски — `vera_shared/db/chunk_vectors.py`**, таблица
`event_chunk_embeddings (event_id, chunk_no, embedding_vec halfvec(1024))`,
PK `(event_id, chunk_no)`, `ON DELETE CASCADE`, HNSW по `binary_quantize`
той же формы, что у событий (`vectors.ann_index_sql` с параметрами таблицы
и имени индекса; DDL — `chunk_schema_sql`, текст миграции 032 сверяется с
ним тестом). Короткие события сюда не пишутся — их вектор, как и раньше,
только в `event_embeddings`. У длинного события есть и вектор события
(по `llm_excerpt`), и куски.

- **Запись** — `brain_triage/chunks.py` `embed_event_chunks`, после
  записи вектора события в `process_pending`: только события, чей вектор
  записан в этом батче; эмбеддинг пачками по 64 куска; куски события
  заменяются целиком (`replace_event_chunks`: `chunk_delete_sql` + `chunk_insert_sql` — сессию Claude дописывают);
  если событие стало коротким — `drop_event_chunks`. Отказ эмбеддинга
  кусков событие не трогает: останется один вектор, триаж не падает.
- **Поиск** — `brain_search/ann.py`: при валидном индексе кусков
  (`chunk_ann_available`, проверка через `vectors.index_is_valid`)
  `ann_rows_sql(where, with_chunks=True)` берёт кандидатов из обоих индексов
  (`UNION ALL`, кусковая часть — `chunk_candidates_sql`), `GROUP BY event_id`
  с `MAX(sim)` — событие получает лучший косинус из своего вектора и своих
  кусков, одна строка на событие, показ — событие. Всё в той же точке
  сохранения с `statement_timeout`. `merge_candidates` поднимает `vec_sim`
  строки основной выборки (FTS/проект/время), если ANN нашёл то же событие
  через кусок лучше.
- **Ограничение:** строка основной выборки, которую ANN в топ-200 не
  принёс, сохраняет косинус вектора события — куски ей не пересчитываются
  (иначе пришлось бы трогать общий `_select` в retrieval.py).
- **До наката 032** (и на SQLite) кусков нет: `chunk_table_available` ложно (кэш на процесс, в тестах сбрасывает `forget_chunk_capability`),
  триаж и поиск работают как до правки.

**Объём (прод 2026-09-13).** Событий длиннее 4000 с вектором — 1741,
11.6 млн символов, ~9.8 тыс. кусков; новых ~490 в месяц (gmail 317,
claude_chat 155), ~3.1 тыс. кусков в месяц при старом потолке, до ~2×
после его подъёма. Токены voyage-4: ~1.5 тыс. символов на кусок → ~0.5
тыс. токенов; месяц — порядка 2–4 млн, разовый бэкфил — ~5 млн. Место:
~2.1 КБ на строку (PLAIN) → ~25 МБ на 10 тыс. кусков + индекс ~2 МБ.

**Отвергнуто.**
- Полный текст в `content_extra` или отдельной таблице — полнотекст и
  превью его не видят, два источника правды; 32 тыс. в `content_text`
  дают ~4 МБ в месяц прироста, GIN-индекс по tsvector на это не реагирует.
- Без потолка — рассылки бывают сотнями КБ HTML-мусора.
- Куски и для коротких событий — дубль вектора и токенов без выигрыша.
- Один вектор по полному тексту (voyage-4 принимает 32 тыс. токенов) —
  именно размывание и есть проблема.

**Бэкфил.** Хвосты, отрезанные до правки, не восстановить — их нет в базе.
Для событий, текст которых сохранился (≤8000, но длиннее порога), —
`scripts/backfill_chunks.py` (`--estimate` печатает события/куски/токены,
без флага — эмбеддит; идёт по id, события с кусками пропускает). Новые и
перезаписанные события режет триаж. Порядок — `deploy-ops.md`, «Куски
длинных событий (032)».


### rel-extract concurrency ceiling

Admitted events are dispatched **fire-and-forget** (`_safe_rel_extract`,
never awaited). The `asyncio.Semaphore` inside `process_pending()` does not
bound them: it is recreated on every call and only wraps the foreground
triage calls, so background tasks accumulated *between* calls. With
`PACE_BETWEEN_S=0.5` and no sleep while there is work, `process_pending()`
cycles every ~1-3s, while one rel-extract can run up to the broker's
`BROKER_JOB_DEADLINE_S` (120s) — dozens of tasks in flight, each opening up
to ~10 DB sessions against a 10-connection pool (`pool_size=3 +
max_overflow=7`) that also serves the claim query, the status writes, the
watchdog and the retry loop. Exhausting it stalls the foreground too.

Two bounds, both in `brain_triage/config.py`:

- `REL_EXTRACT_CONCURRENCY` (`TRIAGE_REL_CONCURRENCY`, default **3**) — a
  module-level semaphore, deliberately *not* per-cycle.
- `REL_EXTRACT_TIMEOUT_S` (`TRIAGE_REL_TIMEOUT_S`, default **180**) — the
  foreground path has had an `asyncio.wait_for` all along
  (`concurrency.py`); the background path had none and relied on the broker
  client's own ceiling. Kept above `JOB_POLL_DEADLINE_S` so it cuts hung
  calls, not healthy slow ones — a test asserts that ordering.

`extract_and_store()` also memoises name→entity within a single event: the
same person usually appears in several facts in a row, and each resolve is
its own session.

## Backfill pause + rate limit

Two controls in the «Конвейер обработки» block on `/sources`, both stored in the
`app_control` KV table (`vera_shared.control`, migration 009), so they
hold across restarts/deploys:

- **⏸ Пауза / ▶ Продолжить** — flips `backfill_paused`. Both
  `brain-triage` `process_pending()` and `media-worker`'s loop check
  `is_backfill_paused()` at the top of each cycle and skip claiming while
  paused. Events stay `pending` / `media_pending` and resume in place.
- **Лимит запросов/час** — `backfill_max_per_hour` (0 = unlimited).
  Even-tempo throttle: the hourly cap is spread to a per-minute budget and
  workers *atomically reserve* their slice via
  `reserve_backfill_allowance(want)` — an `app_control` counter row per
  minute (`backfill_used:<YYYYMMDDHHMM>`, incremented under row-lock, old
  minutes garbage-collected in the same transaction). The old
  read-then-claim helper (removed 2026-07-17)
  raced across replicas: each of the 5 triage replicas read the same
  remaining budget and claimed all of it — up to 5× the intended rate.
  The counter counts *reserved events*; group batching makes actual LLM
  calls fewer, so the reservation is a safe upper bound. The budget is
  global across triage + media + replicas. Live events share the same
  budget, so the cap bounds total throughput.

Live ingest (Telegram/Gmail/IG) is never throttled — only LLM-consuming
processing is paused/paced.

## brain-search

`services/brain-search/src/brain_search/app.py`

- `POST /search` — entry point for the Telegram bot and dashboard.
- Hybrid retrieval: FTS (`russian` OR `indonesian`, ts_rank — see below) AND vector similarity (косинус) over Voyage embeddings.
- `source_url` in search results uses only an existing Slack
  `events.metadata.permalink` whose HTTPS host and channel/timestamp match
  the stored source ID. Missing or mismatched permalinks return `null`: the
  search service does not fabricate workspace or thread URLs. The answer
  context and agent observations carry event IDs and available links, while
  prompts request `[event:ID]` citations. An event reference is not a
  verified LLM citation or proof that the linked source supports the answer;
  post-deploy answer and source checks remain required. Other sources have no
  original link until account-specific resolution is implemented.

### Полнотекст на нескольких языках (`fts.py`, миграция 031)

Все FTS-выражения — в `brain_search/fts.py`: `FTS_CONFIGS`,
`build_ts_query` (слова → `w1:* | w2:*`, одна на `/search` и на агентский
`search_events`: обе идут через `pipeline.query_terms`), `fts_match_sql` (OR
по конфигурациям) и `fts_rank_sql`. retrieval.py только подставляет их.

**Проблема (замер 2026-09-13).** Поиск стоял на одной `russian`. В базе 445
тыс. событий: ~115 тыс. с украинскими буквами, ~31 тыс. без кириллицы
(IT STEP Jakarta, EN/ID). На 5% выборке (22 тыс.) выяснилось:

| слово | `russian` | `indonesian` |
|---|---|---|
| pendaftaran:* | 2 | 15 |
| pembayaran:* | 4 | 9 |
| зустріч:* | 55 | 55 |

Английский `russian` стеммит сама — латиница там идёт в `english_stem`
(payments → payment), стоп-слова английские тоже. Украинский ловится
префиксом русского стема. Не ловится индонезийский.

**Решение.** Вторая конфигурация `indonesian` (snowball, есть в pg16):
латиницу стеммит по-индонезийски (membayar/pembayaran → bayar), кириллицу
оставляет точным токеном — то есть для украинского это `simple` (лексем на
выборке 991 449 против 991 559 у `simple`). Hunspell uk в образе
`pgvector/pgvector:pg16` нет, так что точный токен — лучшее доступное.

Рассмотрено и отвергнуто:
- **(а) объединённый вектор `russian || indonesian (|| simple)`** в одном
  индексе — лексем 1.58× от русского (~196 МБ против 124 МБ), индекс
  строился бы заново рядом со старым; `simple` сверх `indonesian` ничего не
  добавляет.
- **(б) язык события + per-event конфигурация** — нужна колонка с языком и
  бэкфил по горячей таблице (700 МБ heap), а смешанные письма (RU+EN) всё
  равно теряли бы вторую половину.
- **generated column** — переписывает таблицу; только индекс по выражению.

**Ранжирование.** `COALESCE(NULLIF(ts_rank(russian), 0), ts_rank(indonesian), 0)`:
запрос — всегда OR префиксов, и несовпавший документ даёт ровно 0 (проверено
на проде), поэтому строки, которые `russian` находила раньше, получают тот
же rank и тот же взаимный порядок (интеграционный тест сравнивает с
эталонным `ORDER BY ts_rank(russian)`). Найденные только `indonesian`
встают по своему rank — и ранги двух конфигураций между собой **не
откалиброваны**: индонезийское совпадение может встать выше слабого русского
в смешанной выдаче. Это цена покрытия второго языка, а не нарушение
инварианта (порядок внутри русских совпадений сохранён); окончательный порядок
всё равно задаёт скоринг brain-search поверх кандидатов, а не голый `ts_rank`. У `indonesian` в Postgres нет стоп-листа —
служебные слова ru/uk/en/id (`lang.py`, см. ниже) выкидываются до
tsquery, иначе `the:*` матчил бы почти все английские письма.

**Индексы.** `ix_events_fts_russian` (стоял на проде давно, ставился руками;
031 фиксирует его для чистых баз) и новый `ix_events_fts_indonesian`, оба
GIN по выражению, план — `BitmapOr`. Выражение в запросе обязано совпадать
с индексом дословно (тест `test_search_fts.py` сверяет с файлом миграции).
Замер до: FTS-запрос `оплата:* | счет:*` по индексу — 3.5 с (14 тыс.
кандидатов, основное время — heap и пересчёт to_tsvector для ts_rank);
любой другой конфигурации без индекса — parallel seq scan 7.7 с. После —
ожидается BitmapOr, время того же порядка + доля строк второй
конфигурации. Накат — `deploy-ops.md`, «Многоязычный полнотекст».

### Дословная речь в полнотексте (`transcript_text`, миграция 033)

**Отказ.** У голосового события `content_text` — выжимка, и сказанное в
разговоре один раз в неё не попадает. Замер 17.09.2026 по событию 483722
(созвон 118 минут, 3787 реплик, 151 237 символов дословно против 8 287
символов выжимки): из 2786 различных слов длиннее шести букв **2538 (91%)
есть только в стенограмме**. Фамилия «Елиупова», названная в созвоне
однажды, полнотекстом по `content_text` давала 0 строк, а по
`content_extra` — сразу это событие. Векторы не спасали: `brain-triage`
строит их из `llm_excerpt(content_text)`, куски (032) режутся оттуда же —
дословного текста не было **ни в одном индексе**.

**Решение.** Колонка `events.transcript_text` (плоская речь без служебных
ключей jsonb), её пишет шлюз на приёме (`gateway/voice.py::transcript_text`),
плюс два GIN-индекса по выражению — `ix_events_fts_transcript_russian` и
`ix_events_fts_transcript_indonesian`. `fts.py` перебирает
`FTS_COLUMNS × FTS_CONFIGS`, выжимка первой: строка, найденная по
`content_text`, получает ровно прежний ранг, стенограмма ранжирует только
там, где выжимка дала ноль.

**Почему не векторы.** Отказ лексический — имя собственное, сказанное один
раз, а не смысловая близость. Куски всех 1.74 МБ стенограмм дали бы ~1340
строк в `event_chunk_embeddings` (~3.6 МБ) и столько же вызовов эмбеддинга;
GIN по тем же 1.74 МБ стоит единицы мегабайт и **ноль** вызовов. Плюс сотня
обрывков «ага, давай» в ANN-пуле перебивала бы выжимку — ровно та причина,
по которой вектор изначально строится только по ней.

**Почему колонка, а не индекс по выражению из `content_extra`.**
`to_tsvector(cfg, jsonb)` индексирует и значения служебных ключей (`mic`,
`system`, `voice_transcript`) — запрос со словом «system» матчил бы каждое
голосовое событие. `ADD COLUMN` без `DEFAULT` в pg11+ меняет только каталог,
events (706 МБ, 445 тыс. строк) не переписывается.

**Цена.** `transcript_text` заполнен только у голосовых событий: 150 за всю
историю, ~130 в месяц, 1.74 МБ текста всего и ~1.5 МБ в месяц прироста.
`to_tsvector(cfg, NULL)` = NULL, а NULL в GIN не попадает — оба индекса
покрывают сотни строк, не 445 тысяч. Потолок колонки —
`MAX_TRANSCRIPT_CHARS` = 256 тыс. символов (1.7× к самому длинному созвону).

**Старые события остаются с NULL.** Обратное заполнение — отдельная задача:
один `UPDATE` по 150 строкам из `content_extra->'utterances'`, ~1.7 МБ
записи и достройка тех же GIN-индексов, без единого вызова модели.
- ReAct agent loop (`agent.py`):
  - LLM emits strict JSON each step: `{action: 'tool', name, params}` or `{action: 'answer', text}`.
  - Tools available: `search_events`, `memory.remember`, plus everything from `ingestor-telegram` via `/tools/spec` HTTP discovery.
  - Max 6 steps. Returns AnswerResponse with provider, cost, agent_trace.
  - Each step is wrapped in `asyncio.wait_for(..., timeout=AGENT_STEP_TIMEOUT_S)`
    (default 90s, env-overridable) — a hung broker call used to block
    `/search` indefinitely; now it returns "LLM не ответил вовремя" instead.

**Answer evidence (2026-10):** `evidence_excerpt()` builds the bounded source text. The API result card still shows a 400-character
preview, but synthesis uses a bounded excerpt of the stored event text: up to
4,000 characters for the first three hits and 900 for later hits. Long excerpts
keep their beginning and end and mark the omitted middle. The agent search tool
uses the same excerpt rule. The next agent step serializes complete compact
event cards with ID, source, excerpt, and available link inside a 3,000-character
observation budget. If a whole card does not fit, `omitted_events` counts it;
no excerpt is sent without its event ID. Prompts require the model to preserve source
causality and identifier types and to avoid claiming that a detail is absent
when only an excerpt was read. The underlying source can still be incomplete;
absence claims require checking the full event or original.
An adjacent login alert is not evidence for the cause of a payment restriction;
answers to a stated-reason question omit unrequested causal hypotheses unless
the source explicitly connects the events. Explicit requests for hypotheses
remain supported: label possibilities unverified, separate them from the stated
reason, and say what evidence is missing. If an email's local Date header and
the event UTC timestamp fall on different calendar days, name the time zone.

`explicit_event_ids()` recognizes an explicit `event:ID` or `событие ID` query;
the direct ID path reads up to five named IDs from visible legacy `events`,
applying the same visibility, source, people, date, and project filters as ordinary
retrieval, including structured account restrictions, but bypassing FTS and embeddings.
Unmarked numbers require at least five digits so a year cannot become an ID;
four-digit IDs need their own `event:` or `событие ID` marker. This lets an explicitly named
stored event be read regardless of its ranked-search position. The request limit still caps
returned IDs. A missing result means only that the event was not found among
accessible records for this request; it does not prove nonexistence upstream.
Ordinary ranked search remains a bounded sample and cannot establish that an
unreturned event does not exist.

For one explicitly requested quotation (`phrase "…"`, `quote "…"`, `фразу «…»`,
`цитату “…”`), `split_quoted_query()` separates the quoted search text from the
surrounding scope and answer instructions. A query consisting entirely of one
quotation uses the same path. The quotation supplies FTS terms and the embedding;
surrounding words cannot introduce an inferred account ranking bonus. Quoted dates,
project names, summary/report words and event-ID markers do not set search scope,
report mode, or a direct event read; dates,
projects, and summary/report intent outside the quotation still apply. Inferred
project trigger removal does not discard those words from the quotation itself.

Typed source/account/date/people filters remain AND restrictions before retrieval
and ANN fusion. This parser does not infer source, account, or chat identity from
free text: callers must pass supported structured filters for strict scoping.
The internal `/search` model supports account/project in `filters`; MCP `search`
supports source/start/end/people/kind, while the gateway `/v1/search` bridge currently
accepts only q/limit/use_agent. The external ChatGPT wrapper is outside this repo.

Any additional quotation (including a quoted chat name), unmatched/nested quote
delimiters, escaped/newline quotations, quotations longer than 500 characters, and
recognized negative wording outside the quotation retain the previous search
semantics. Exclusion predicates are not implemented; fallback does not guarantee
an exclusion search. A leading quotation followed by prose without a phrase/quote
marker also uses the previous path. Ordinary FTS still uses
OR prefixes, ANN still contributes its bounded pool, and numeric scoring/topK are
unchanged. This removes demonstrated context pollution; it does not guarantee that
every exact phrase in the database is retrieved or that quoted words match contiguously.
The regression tests reproduce a quote lost at topK 3/10 through an inferred account
bonus in an otherwise identical semantic pool, and quote content incorrectly setting
date/project scope. PostgreSQL FTS/rank and production performance need separate
integration validation before rollout. `test_quoted_search_pg.py` uses the existing CI
PostgreSQL, temporary rows and rollback to compare short/long FTS results at topK 3/10
while excluding another source/account, hidden rows and out-of-window rows. It performs
no schema reset or permanent-table writes. Local absence of PostgreSQL is a skipped test,
not a successful SQL verification.

For a recognized quotation with no selected candidates and `use_agent=false`, the
response is deterministic: search returned no accessible records, and this bounded
selection does not prove that the quotation is absent from original messages.
No LLM call is made for that empty result. Agent mode retains further tool search;
its answer remains governed by the limited-selection evidence rules.

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

### Качество поиска (ревью 2026-10-03, ветка `fix/search-quality`)

Десять вопросов на четырёх языках оценивались по top-5; ниже — что починено
и почему.

**Замер на проде** (read-only: те же 10 вопросов, новый код из `/tmp`
контейнера brain-search, старый сервис не трогали). Доля источников в
суммарном top-5 (50 мест): `vera_memory` 23 → 6, telegram 13 → 20, gmail
12 → 17; сообщений ботов `stepanv2bot`/`itSTEPan_bot` 4 → 0. «Проектный»
вопрос про кальяны Веранды раньше отдавал свежие записи памяти и
нерелевантный чат, теперь — сообщения «Veranda менеджмент» с договором.
Цена снижения веса памяти: короткий выведенный факт, который был лучшим
ответом (например, платёжный профиль Google Ads), теперь стоит наравне с
письмами и может выпасть из top-5 — дальше его нужно подтягивать
значимостью, а не множителем.

**Модули brain-search.**

| модуль | роль |
|---|---|
| `lang.py` | `STOPWORDS` ru/uk/en/id, `is_stopword`, `content_words` — единственный список служебных слов |
| `pipeline.py` | `query_terms` (tsquery + имена для account), `embed_query`, `search_ranked` — общий путь `/search` и инструмента агента |
| `retrieval.py` | режимы выборки (`fetch_candidates`) |
| `retrieval_filters.py` | WHERE-фрагменты: `project_clause`, `account_clause`, `semantic_filter`, `source_clause`, `NOT_A_WORLD_EVENT` |
| `rows.py` | `Candidate` (NamedTuple строки выборки) и `META_COLUMNS` |
| `scoring.py` | `score_candidates` (строки) и `score_rows` (превью для ответа) |
| `agent.py` / `agent_tools.py` | цикл ReAct / инструменты, их модели аргументов и `execute_tool` |

**Служебные слова.** «bagaimana», «який», «who», «what» в OR-запросе
расширяли выборку на пол-корпуса (`bagaimana:*`). `lang.py` объединяет
стоп-слова четырёх языков; список знает и `build_ts_query`. Дефис в
токенизатор не входит: он служебный символ tsquery.

**Проектный режим.** Раньше `ORDER BY occurred_at DESC` без FTS: запрос
«договор с поставщиком кальянов Веранда» получал 200 свежих сообщений
проекта вне зависимости от слов. Теперь внутри фильтра проекта работают
`fts_match_sql`/`fts_rank_sql` (и ANN-добавка, как везде); свежее — только
если содержательных слов нет или они ничего не нашли. Слова-триггеры самого
проекта («Веранда») в tsquery не попадают: внутри проекта они есть везде.
Проект определяет `vera_shared.projects.rules` (`QUERY_TRIGGERS`,
`project_from_query`) — те же правила, что у `sync_projects`; своих реестров
ящиков и чатов (`PROJECT_ALIASES`, ILIKE по account, `chat_title = ANY`) у
поиска больше нет: колонка `events.project` заполнена у 99.8% событий
(532 из 471 тыс. — NULL; замер 2026-10-03).

**Веса.** `vera_memory` был 1.2 и вместе с `importance/200` и бонусом account
обгонял первичные события в 7 запросах из 10 — стал 1.0 (`SOURCE_WEIGHTS`
хранит только понижающие). Агентская `memory.remember` пишет
`metadata.written_by = "search_agent"` (`AGENT_WRITER`) и ровно того же
веса, что первичное событие, не выше. Авторы-боты (`sender_username` на
`bot`: autopay_telebot, stepanv2bot, itSTEPan_bot, ChatKeeperBot…)
умножаются на `BOT_AUTHOR_WEIGHT` = 0.4, но не исключаются: в чате
«Veranda transactions» бот оплат — единственный автор.

**Агент.** `search_events` — не вторая реализация поиска, а вызов
`pipeline.search_ranked` (ANN, куски, скоринг, фильтр `source`): повторный
поиск равен первому. Аргументы инструментов — pydantic-модели
`SearchEventsArgs` и `RememberArgs` с границами (`limit` ≤ `MAX_SEARCH_LIMIT`
= 50, `fact` ≤ 2000 знаков, ≤ 10 тегов), лишние ключи игнорируются, ошибка
аргументов или исполнения возвращается наблюдением (`execute_tool`), а не
500: текст писем, попавший в промпт, не должен ронять `/search` или писать
в память что угодно. `SearchQuery.limit` ≤ 100, `max_steps` ≤ 10;
мёртвое `days_back` удалено. Описания инструментов (`ToolDescriptor`)
встроенные плюс `load_remote_tool_specs` из `/tools/spec` ingestor-telegram;
даты `date_from`/`date_to` разбирает `parse_iso_date`, окно локальных дней
(Jakarta, включительно) строит `date_window`.
`search_events.source` принимает `telegram`, `gmail`, `instagram`, `slack`,
`vera_chat` и `any` (по умолчанию). Slack разрешён в `SearchEventsArgs`,
описании инструмента и JSON-схеме `BUILTIN_SPECS`, которую видит агент;
значение передаётся без замены в общий `pipeline.search_ranked` вместе с датами
и лимитом. Это исправляет отклонение `source="slack"` при проверке аргументов;
полнота выдачи и ложные отрицательные ответы требуют отдельной проверки.

**Даты.** `1.5 млн`, `3.5`, `v1.2.3` больше не даты: `dd.mm` без года
требует две цифры («09.06»). Относительные дни — вчера/сегодня/позавчера на
ru/uk/en/id («yesterday», «kemarin», «hari ini», «вчора», «сьогодні»).

### Точные идентификаторы и авторство в синтезе (2026-10-09)

**Тикет-идентификаторы** (`brain_search/identifiers.py`, `identifier_rows.py`).
Запрос «SIN-4905» уходил в tsquery как `sin:* | 4905:*`; на проде `sin:*`
даёт 4863 события, а события 510716 и 508984 (содержат `SIN-4905`
дословно) не попадали даже в топ-10 по ts_rank — отсюда «упоминаний нет»
на limit=3 и ранги 6–7 на limit=10. Теперь вопрос с токеном
`[A-Za-z]{2,10}-\d{2,7}` (до 5 штук; префиксы из `NON_TICKET_PREFIXES` —
utf, gpt, covid, iso, sha … — не считаются) получает отдельную выборку:
`phraseto_tsquery(<конфигурация>, 'SIN-4905')` по тем же GIN-индексам
(`fts_phrase_match_sql`) плюс regex-границы `~*` (XSIN-4905 и SIN-49055 не
совпадают), до 30 событий на идентификатор, под тем же фильтром
(скрытые/проект/окно/источник/links). Ручная строка `sin <-> 4905` не
работает: парсер токенизирует `SIN-4905` как `'sin' <-> '-4905'` (число со
знаком), а `phraseto_tsquery` повторяет это сам. Строки получают `rank=100`
(`IDENTIFIER_RANK`) и после скоринга стоят выше любого семантического
кандидата; между собой точные попадания идут по `occurred_at` от новых к
старым (в `score_candidates`), а не по importance/косинусу: при равном
`rank` лимит иначе отрезал свежее обновление тикета (SIN-4885, событие
510802). Дубль из основной выборки сохраняет свой косинус. Действует и в
`/search`, и в агентском `search_events` (`pipeline.search_ranked`).
Если идентификатор найден дословно в результатах, в промпт (хвост, `notes`)
добавляется указание «НЕЛЬЗЯ отвечать "не найдено"» со ссылками на события —
только для идентификаторов, реально найденных дословно (`identifier_hits`).
В агентском пути то же указание приходит полем `identifier_note` в результате
`search_events`. `synthesis.build_context` собирает блоки событий с
`author=…` (публичное имя, покрыто тестами).

**Авторство и модальность** (`brain_search/authorship.py`). В колонки
кандидата добавлен `metadata->>'direction'`. Заголовок каждого события в
контексте синтеза несёт `author=<author_label> (<author_role>, входящее или
исходящее)` из metadata, а не из ящика владельца: комментарий Jira от Igor
Gladkyi (событие 510732, входящее) раньше пересказывался как «Ты обозначил».
Правила (`AUTHORSHIP_RULES`): высказывание принадлежит указанному автору,
«ты» — только при `author_role=self` или исходящем; требование, план, условие
(«до решения поток 2 не включаем») нельзя превращать в факт («выключен»).
Правила стоят в стабильной части промпта (и в `SYSTEM_PROMPT` агента);
`build_prompt` теперь ставит все инструкции перед конфигурацией, историей,
вопросом и событиями — переменное в хвосте, префикс кэшируется.
Функции: `ticket_ids` вынимает идентификаторы из вопроса, `identifier_sql`
строит выборку, `fetch_identifier_rows` её выполняет, `merge_identifier_rows`
вливает строки в кандидатов; `author_tag` формирует метку `author=…`.
**Снятый текст и статус «готово».** `AUTHORSHIP_RULES` дополнены двумя
правилами (в том же стабильном префиксе): текст в `[удалено: …]` или
зачёркнутый — прежняя версия, не текущее требование (LAM-264, событие
510796: «платформа выплачивает академиям» выдавалось за действующее);
`Ready to Prod / Ready to Test` и «на прод» — готовность, а не выкатка
(SIN-4885, событие 510802: «выведена на прод»); «выкачено/завершено» — только
при деплое, release, Done/Closed. То же правило одной фразой стоит в
триаж-промпте для `signals.summary`. Метки `[удалено: …]` ставит инжестор gmail
(см. sources.md, `html_text.py`).
**Хэши коммитов** (событие 510826, «failed Deploy vera3 … 4398a6d»). Токен из
7–40 hex-символов с цифрой и буквой a-f (`commit_hashes`) идёт тем же
точным путём, что тикет (`exact_identifiers` = тикеты + хэши). FTS на проде
токенизирует `4398a6d` одним токеном, поэтому префильтр — `to_tsquery(h:*)`
по тем же GIN-индексам (префикс ловит и 40-символьный хэш), затем regex с
границами `[^0-9a-f]` и хвостом `[0-9a-f]{0,33}` (`MAX_HASH_SUFFIX`); EXPLAIN
ANALYZE на проде ~114 мс. `is_hash` отличает хэш от тикета. Если вопрос — по
сути один идентификатор (`is_identifier_only`) и точного попадания нет
(`has_exact_rows`), `/search` отвечает `NO_EXACT_ANSWER` («точных упоминаний не
нашла», не «записи нет») без источников, агентский `search_events` возвращает
пусто; в остальных вопросах `identifier_miss_note` запрещает категоричное «нет».
Тесты: `test_search_identifiers_authorship.py`, `test_gmail_diff_markup.py`,
`test_search_commit_hash.py`.

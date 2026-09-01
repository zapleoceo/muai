# API

## Gateway (`vera3-gateway`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/event/{source}` | POST | `X-Internal-Secret` | Ingest endpoint — dedupes by `source_event_id` |
| `/webhook/{source}` | POST | source-specific | Webhook receiver (Telegram, etc.) |
| `/v1/claude/remember` | POST | `X-Internal-Secret` | Fact ingest from Claude conversations. Two-layer dedup: exact sha256 of text + semantic cosine ≥ 0.92 over last 7 days of claude-source events. Body: `{text, kind: "fact"\|"decision"\|"todo"\|"preference", context?, tags?}`. Returns `{ok, event_id, deduped, dedup_reason: "exact"\|"semantic"\|null, similar_event_id?, similarity?}`. The dedup embedding is written into `event_embeddings` immediately on accept (2026-07-17) — closes the blind window where two similar facts saved minutes apart both passed semantic dedup because triage hadn't embedded the first one yet. Called by the `vera-mcp` MCP server (see `mcp-claude.md`). |
| `/v1/voice/session` | POST | `X-Internal-Secret` | Разговор с ноутбука: расшифровка одной сессии → выжимка в `events` (source=`voice`). **Дословный текст не сохраняется** — он нужен только для одного осмысления (`chat:smart`, strict json_schema) и остаётся на ноутбуке. Тело: `{started_at, ended_at, app, window_title, device_hint, meeting_id?, part?, utterances:[{at, stream: mic|system, text}]}`. Длинная расшифровка **сворачивается по окнам, а не обрезается** (`gateway/voice_distill.py`): каждое окно осмысляется отдельно, второй проход сливает частичные выжимки в одну. Дедуп по `started_at+app+window_title`, поэтому ретрай из офлайн-очереди не двоит. Сбой брокера не теряет событие — сохраняется факт разговора с метаданными. Клиент: `vera-listener/` |

The body of `/event/<source>` is a `RawEvent` (`shared/vera_shared/events/schema.py`).
Note that the ingestors do NOT go through this endpoint — they write via
`vera_shared.ingest.insert_events()`; see [sources.md](./sources.md). The
endpoint serves webhooks and the bot's `vera_chat` writes.

```json
{
  "source": "telegram",
  "source_event_id": "tg:<chat>:<msg>",
  "account": "userbot",
  "category": "user|channel|group",
  "content_text": "...",
  "occurred_at": "2026-06-24T07:00:00",
  "metadata": { "chat_id": ..., "direction": "sent|received" }
}
```

### Internal auth (`vera_shared/auth.py::internal_secret_ok`)

Every gateway route that reads or writes data (`/event/*`, `/v1/claude/*`,
`/v1/search`, `/v1/events/*`, `/v1/entity/*`, `/api/events/{id}`) requires
the `X-Internal-Secret` header, checked by
`gateway/auth.py::check_internal_secret()`. `/healthz` is the only
unauthenticated route.

The comparison itself lives in **`vera_shared.auth.internal_secret_ok()`** —
gateway and brain-search each keep a thin `check_internal_secret()` wrapper
that turns `False` into their own `HTTPException(401)`, because the services
don't import each other and `vera_shared` deliberately doesn't depend on
FastAPI. Two properties are the reason it's one function:

- **Fail-closed.** If `INTERNAL_SECRET` is unset/empty, every request is
  rejected rather than let through. `docker-compose.yml` marks the var
  required (`INTERNAL_SECRET:?...`), so a misconfigured non-compose deploy
  locks down instead of exposing event bodies.
- **Constant-time.** `hmac.compare_digest`, not `!=`. Both copies used to
  compare with `!=`, whose runtime depends on the length of the matching
  prefix — the ports are loopback-only so this was never practically
  exploitable, but `dashboard/auth.py` already did it right and there was no
  reason for these two to differ.

## Brain Search (`vera3-brain-search`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/search` | POST | `X-Internal-Secret` | Hybrid retrieval + agent loop |

`/search` requires the same `X-Internal-Secret` header as the gateway,
checked by brain-search's fail-closed `check_internal_secret()` wrapper over
the shared `internal_secret_ok()` (the port is published on the host's
127.0.0.1, so any local process could otherwise query the whole memory).
Callers — bot-telegram, dashboard
`/search-ui`, gateway `/v1/search` proxy — all send the header.

The gateway's `MaxBodySizeMiddleware` also rejects POST/PUT/PATCH without
a `Content-Length` header (HTTP 411): chunked transfer-encoding used to
bypass the 2MB body cap entirely.

`POST /search` body:

```json
{
  "q": "сколько событий за неделю",
  "limit": 15,
  "use_agent": true,
  "max_steps": 6,
  "conversation": { "chat_id": 169510539 }
}
```

Returns `AnswerResponse` with `answer`, `results`, `provider`, `cost_usd`,
`agent_steps`, `agent_trace`.

## Dashboard (`vera3-dashboard`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/login` | GET | none | TG Login Widget |
| `/api/tg_login` | GET | TG widget signature | Callback → session cookie |
| `/logout` | GET | none | Clear cookie |
| `/` | GET | owner cookie | Home — cards, live progress |
| `/events` | GET | owner cookie | Event browser with filters. Nav label is "log" — per-event columns show the broker call that triaged it (`request_id`/model/tokens/cost, via `usage_log`); batch-triaged events show "в пачке ✓" instead of a blank (see `domain-model.md`) |
| `/sources` | GET | owner cookie | Список источников — состояние потока, объём, действие. Строится из `source_registry`, не из ручной разметки |
| `/sources/{key}` | GET | owner cookie | Подробности источника: подключение, разбивки от провайдера `source_detail`. Источник без провайдера так и говорит |
| `/api/slack/start` | GET | owner cookie | Форма ввода user-токена Slack (`slack_start_form`) — со списком нужных прав |
| `/api/slack/start` | POST | owner cookie | Проверка токена через `auth.test` и сохранение в `slack_auth` под шифрованием (`slack_start`). Токен не логируется и в ответ не возвращается |
| `/api/sources/{key}/disconnect` | GET | owner cookie | Подтверждение отключения (`disconnect_confirm`): что именно погаснет и что события останутся |
| `/api/sources/{key}/disconnect` | POST | owner cookie | Погасить строки доступа источника (`disconnect_apply`). Секрет НЕ удаляется — шаг обратим |
| `/graph` | GET | owner cookie | Knowledge-graph visualizer page (`graph_page`) — Cytoscape.js force layout of entities+relationships. See "Graph visualizer" below. |
| `/api/graph` | GET | owner cookie | Node/edge JSON for the visualizer (`graph_data`). Params: `min_degree`, `limit` (≤800), `predicate`, `focus` (entity id), `q` (name→focus). |
| `/api/instagram/start` | GET | owner cookie | Instagram login form (`instagram_start_form`) |
| `/api/instagram/start` | POST | owner cookie | Submit username/password (`instagram_start`) — may return a 2FA/challenge code form |
| `/api/instagram/verify` | POST | owner cookie | Submit 2FA/challenge code (`instagram_verify`) → saves encrypted session |
| `/tokens` | GET | owner cookie | Now redirects to AIbroker — see `llm-broker.md` |
| `/entities/merge-email-dupes` | POST | owner cookie | Слить дубли по рабочему email (`entities_merge_email_dupes`) — детерминированные пары, группы 3+ не трогаются |
| `/search-ui` | POST | owner cookie | "Ask Vera" form handler |

### Graph visualizer (`dashboard/graph_routes.py`)

`/graph` renders Vera's L1 substrate (entities + relationships) as an
interactive force-directed graph via Cytoscape.js (CDN, same pattern as
htmx). The full graph is ~8k entities / ~7k edges — a hairball if drawn at
once, and ~6k of those entities have no relationships at all — so it never
renders "everything":

- Default = the **connected core**: `repo.graph_snapshot(min_degree, limit)`
  returns the top-`limit` (≤`GRAPH_MAX_NODES`=800) entities by degree with
  degree ≥ `min_degree`, plus every edge whose both endpoints are in that
  set (so the client never references a missing node).
- Tap a node (or search by name) → **ego network**: `graph_snapshot(focus_id)`
  returns that entity + its 1-hop neighbours. Name search resolves via the
  fuzzy `find_entity_by_name`.
- `predicate` filter narrows to one relationship type. Node colour = entity
  type (person / group / channel), size ∝ degree.

Two different degrees are in play, deliberately: node **selection** uses the
degree *within the active predicate filter* (the `degree` CTE), while the
degree **shown on a node** is its total — every relationship plus every
current membership, both sides, unfiltered. The displayed number answers
"how connected is this person", not "how many edges survived the filter".

That total is one grouped query over the returned id set. It used to be two
correlated subqueries per row, i.e. up to 800 × 2 per page render, and half
of them keyed on `memberships.child_entity_id`, which had no index at all —
`ix_membership_child`, migration 028. Both sides of `relationships` were
already indexed; memberships only had the parent side, and `uq_membership`
couldn't stand in for it because `parent_entity_id` leads that constraint.

All graph SQL lives in the `vera_shared.graph` package; **no service
reaches past it**. `gateway/query.py` used to join `relationships` with
`entities` inside the route function and `ingestor_telegram/roster_sync.py`
joined `entity_aliases` with `entities` in the worker — both now call
`repo.list_relationships()` / `repo.find_project_chats()`, and
`tests/unit/test_graph_boundary.py` fails the build if a service grows raw
SQL against a graph table again. Routes only shape JSON / HTML.

Within the `graph/` package itself raw SQL is fine and deliberate:
`merge_entities`, collision handling and dossiers are not expressible as
repository CRUD, and wrapping them would hide transactional logic. The
point of the boundary is that swapping the store is an edit to one package. `IN` clauses use expanding bindparams
so the queries run on both Postgres (prod) and SQLite (tests).

### Stats caching (`dashboard/stats.py`)

`/` and `/sources` used to run ~15-17 heavy `COUNT`/`GROUP BY` scans per
page load (and again every `/_progress` poll) — the main cause of slow
page loads before this. `get_stats()` collapses
those into ~2 scans total via `FILTER` aggregates, cached for
`TTL_S=60` with stale-while-revalidate (`_serve_cached`): a stale value
is returned instantly while a background refresh (`_bg_refresh`) runs, so
the heavy scan almost never blocks a request. `cache_age_s()` reports how
old the cached value is, for the "updated N sec ago" note in the UI.

Страница источников разделена на два уровня, и ни один не знает имён
источников: `get_sources_overview()` — один `GROUP BY source` на весь список,
`get_source_detail(key)` — разбивки одного источника, по требованию и с
отдельным кэшем на каждый (скан по 400 тыс. строк telegram незачем повторять
на каждый показ). `drop_detail_cache(key)` сбрасывает кэш после
переподключения, иначе страница ещё минуту показывала бы «не подключено».
Сами разбивки собирает `blocks_for()` из `source_detail`.

## Ingestor-telegram tools (`vera3-ingestor-telegram`, port 8000)

X-Internal-Secret required on all `/tools/*`.

| Path | Method | Description |
|---|---|---|
| `/healthz` | GET | Liveness |
| `/tools/spec` | GET | JSON-Schema list (consumed by agent loop) |
| `/tools/list_dialogs` | POST | `{q?, limit?}` |
| `/tools/get_chat_info` | POST | `{chat_query}` |
| `/tools/get_participants` | POST | `{chat_query, limit?}` |
| `/tools/get_dialog_history` | POST | `{chat_query, limit?}` |
| `/tools/find_user` | POST | `{q}` |

## External (via Cloudflare → nginx :80 → :8003 dashboard)

Production URL: `https://dima.veranda.my`

nginx проксирует наружу не только дашборд: `/` → dashboard:8003, а `/event/`,
`/v1/` и `/webhook/` → gateway:8001. Именно поэтому ноутбук может слать
события и голосовые сессии по HTTPS — под `X-Internal-Secret`, без VPN и
туннелей. Всё остальное (brain-search, postgres) слушает только 127.0.0.1.

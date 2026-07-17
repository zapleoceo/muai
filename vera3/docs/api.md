# API

## Gateway (`vera3-gateway`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/event/{source}` | POST | `X-Internal-Secret` | Ingest endpoint — dedupes by `source_event_id` |
| `/webhook/{source}` | POST | source-specific | Webhook receiver (Telegram, etc.) |
| `/v1/claude/remember` | POST | `X-Internal-Secret` | Fact ingest from Claude conversations. Two-layer dedup: exact sha256 of text + semantic cosine ≥ 0.92 over last 7 days of claude-source events. Body: `{text, kind: "fact"\|"decision"\|"todo"\|"preference", context?, tags?}`. Returns `{ok, event_id, deduped, dedup_reason: "exact"\|"semantic"\|null, similar_event_id?, similarity?}`. The dedup embedding is written into `event_embeddings` immediately on accept (2026-07-17) — closes the blind window where two similar facts saved minutes apart both passed semantic dedup because triage hadn't embedded the first one yet. Called by the `vera-mcp` MCP server (see `mcp-claude.md`). |

The body of `/event/<source>` is an EventEnvelope:

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

### Internal auth (`gateway/auth.py::check_internal_secret`)

Every gateway route that reads or writes data (`/event/*`, `/v1/claude/*`,
`/v1/search`, `/v1/events/*`, `/v1/entity/*`, `/api/events/{id}`) requires
the `X-Internal-Secret` header, checked by the single shared
`check_internal_secret()` helper. It is **fail-closed**: if
`INTERNAL_SECRET` is unset/empty, every request is rejected (401) rather
than let through. `docker-compose.yml` marks the var required
(`INTERNAL_SECRET:?...`), so a misconfigured non-compose deploy locks down
instead of exposing event bodies. `/healthz` is the only unauthenticated
route.

## Brain Search (`vera3-brain-search`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/search` | POST | `X-Internal-Secret` | Hybrid retrieval + agent loop |

`/search` requires the same `X-Internal-Secret` header as the gateway,
checked by brain-search's own fail-closed `check_internal_secret()` (the
port is published on the host's 127.0.0.1, so any local process could
otherwise query the whole memory). Callers — bot-telegram, dashboard
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
| `/sources` | GET | owner cookie | Per-source health (telegram/gmail/instagram) |
| `/graph` | GET | owner cookie | Knowledge-graph visualizer page (`graph_page`) — Cytoscape.js force layout of entities+relationships. See "Graph visualizer" below. |
| `/api/graph` | GET | owner cookie | Node/edge JSON for the visualizer (`graph_data`). Params: `min_degree`, `limit` (≤800), `predicate`, `focus` (entity id), `q` (name→focus). |
| `/api/instagram/start` | GET | owner cookie | Instagram login form (`instagram_start_form`) |
| `/api/instagram/start` | POST | owner cookie | Submit username/password (`instagram_start`) — may return a 2FA/challenge code form |
| `/api/instagram/verify` | POST | owner cookie | Submit 2FA/challenge code (`instagram_verify`) → saves encrypted session |
| `/tokens` | GET | owner cookie | Now redirects to AIbroker — see `llm-broker.md` |
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

All graph SQL lives in `vera_shared.graph.repo` (the repository layer);
the route only shapes JSON / HTML. `IN` clauses use expanding bindparams
so the queries run on both Postgres (prod) and SQLite (tests).

### Stats caching (`dashboard/stats.py`)

`/` and `/sources` used to run ~15-17 heavy `COUNT`/`GROUP BY` scans per
page load (and again every `/_progress` poll) — the main cause of slow
page loads before this. `get_stats()` / `get_sources_stats()` collapse
those into ~2 scans total via `FILTER` aggregates, cached for
`TTL_S=60` with stale-while-revalidate (`_serve_cached`): a stale value
is returned instantly while a background refresh (`_bg_refresh`) runs, so
the heavy scan almost never blocks a request. `cache_age_s()` reports how
old the cached value is, for the "updated N sec ago" note in the UI.

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

All routes here are dashboard routes — no other service is exposed.

# API

## Gateway (`vera3-gateway`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/event/{source}` | POST | `X-Internal-Secret` | Ingest endpoint — dedupes by `source_event_id` |
| `/webhook/{source}` | POST | source-specific | Webhook receiver (Telegram, etc.) |
| `/v1/claude/remember` | POST | `X-Internal-Secret` | Fact ingest from Claude conversations. Two-layer dedup: exact sha256 of text + semantic cosine ≥ 0.92 over last 7 days of claude-source events. Body: `{text, kind: "fact"\|"decision"\|"todo"\|"preference", context?, tags?}`. Returns `{ok, event_id, deduped, dedup_reason: "exact"\|"semantic"\|null, similar_event_id?, similarity?}`. Called by the `vera-mcp` MCP server (see `mcp-claude.md`). |

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

## Brain Search (`vera3-brain-search`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/search` | POST | none (internal) | Hybrid retrieval + agent loop |

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
| `/events` | GET | owner cookie | Event browser with filters |
| `/sources` | GET | owner cookie | Per-source health (telegram/gmail/instagram) |
| `/tokens` | GET | owner cookie | Now redirects to AIbroker — see `llm-broker.md` |
| `/search-ui` | POST | owner cookie | "Ask Vera" form handler |

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

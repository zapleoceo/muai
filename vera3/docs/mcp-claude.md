# MCP bridge: Claude ↔ Vera

`vera-mcp` is a stdio MCP server that lets Claude (Code, Desktop, any
MCP host) write facts to and search Vera's brain. Set up 2026-06-28.

## What's exposed

| Tool | What it does | Endpoint |
|---|---|---|
| `vera_remember(text, kind, context?, tags?)` | Write a fact / decision / todo / preference into Vera's events table with `source='claude'`. Two-layer dedup (exact sha256 + semantic cosine ≥0.92, 7-day window). | `POST /v1/claude/remember` |
| `vera_recall(query, limit?)` | Semantic search across the whole brain (every gmail/tg/ig event + previous Claude facts). | `POST /v1/search` (TODO: not yet exposed via gateway) |
| `vera_recent(hours?, source?)` | Recent events, optionally filtered by source. | `GET /v1/events/recent` (TODO) |
| `vera_context(entity_name)` | Everything Vera knows about a named person/project (graph traversal + recent mentions). | `GET /v1/entity/context` (TODO) |

Currently **only `vera_remember` works end-to-end** — the other three
return a `not-implemented-yet` JSON message so Claude doesn't hang. The
search tools will start working once the gateway exposes those endpoints
(they exist inside `brain-search` already, just need a thin proxy).

## How it talks to Vera

```
Claude (any client)
  │ MCP stdio
  ▼
vera-mcp server (Python, uv-managed deps)
  │ HTTPS + X-Internal-Secret
  ▼
nginx (/v1/) → vera3-gateway :8001
  │
  ▼
gateway/claude.py /v1/claude/remember
  │ ├─ exact dedup via UNIQUE (source, source_event_id)
  │ └─ semantic dedup via embed_via_broker + cosine NN
  ▼
events table (source='claude', triage_status='pending')
  │
  ▼
brain-triage picks up → embedding + entity extraction
```

## Local setup (single user, one machine)

1. **Server script** at `~/.claude/mcp-servers/vera-mcp/server.py`.
   Self-contained — PEP 723 inline metadata declares `mcp>=1.0` and
   `httpx>=0.27`. `uv` resolves and caches automatically on first run.

2. **MCP config** at `~/.claude/mcp.json`:
   ```json
   {
     "mcpServers": {
       "vera": {
         "command": "uv",
         "args": ["run", "--script", "<absolute path to server.py>"],
         "env": {
           "VERA_URL": "https://dima.veranda.my",
           "VERA_INTERNAL_SECRET": "<INTERNAL_SECRET from server .env>",
           "VERA_TIMEOUT_S": "30"
         }
       }
     }
   }
   ```
   `~/.claude/mcp.json` is **not** in any git repo, so the secret stays
   local. If the secret leaks, attacker can only POST to
   `/v1/claude/remember` and add events (write-only, no read).

3. **When-to-call rule** in `~/.claude/CLAUDE.md` — a paragraph telling
   Claude to invoke `vera_remember` ONCE at the end of substantive
   turns producing a decision / fact / preference. Examples of what
   counts vs. what doesn't. Without the rule Claude has the tool but
   never uses it.

4. **Restart Claude Code** to load the new MCP server (one-time after
   config changes).

## What gets remembered, what doesn't

**Counted** (worth `vera_remember`):
- Project decisions ("выбрали MCP-вариант A, не Б")
- User preferences ("Дима всегда хочет код-комменты на английском")
- Promises / TODOs that aren't in another tracker
- Facts learned about people/places during conversation
- Important context surrounding work ("делаем потому что Q4 заморозка")

**Skipped** (don't call `vera_remember`):
- Acknowledgements ("ok", "понял")
- Intermediate dev steps (git commits/pushes — git remembers)
- Anything already in Gmail/Telegram (ingestors handle it — would dup)
- Per-line documentation (that's in docs, not memory)
- Speculation ("could be that…") — only verified facts

## Dedup guarantees

Two layers, both server-side. MCP client just POSTs, server decides:

1. **Exact** — `sha256(text.strip())[:16]` is the `source_event_id`.
   DB has `UNIQUE (source='claude', source_event_id)`. Same text twice
   → second POST hits `ON CONFLICT DO NOTHING`, returns
   `{deduped: true, dedup_reason: "exact"}` with the original id.

2. **Semantic** — after exact passes, we embed text via the broker and
   SELECT last 500 claude-source events from the last 7 days with
   non-null embedding. Cosine ≥ `SEMANTIC_DEDUP_THRESHOLD` (0.92) →
   the just-inserted row gets `triage_status='superseded'` with
   `triage_metadata={superseded_by, similarity}` and we return
   `{deduped: true, dedup_reason: "semantic", similar_event_id, similarity}`.

If the broker is down, semantic check is skipped (returns None) — exact
dedup still works, the event lands as new. No crash.

Verified live 2026-06-28: paraphrase "Vera полностью на брокере через
MCP протокол с дедупом" vs. "Vera полностью на брокере через MCP
протокол с дедупом!" → sim=0.984, superseded correctly.

## Storage

Goes into the regular `events` table with `source='claude'`,
`category=kind`, `metadata_={kind, context?, tags?}`,
`occurred_at=now`. No new tables. Triage handles it like any other
event → embedding + entity extraction → graph linkage.

This means `vera_recall` searches Claude-facts alongside emails and
TG messages, which is the point.

## Cost

Per `vera_remember`: ~1 Voyage embedding call (~1k tokens, sub-cent on
Voyage free tier) for the semantic check. Triage adds another embedding
later. Both go through the broker. At 5-10 facts/day this is rounding error.

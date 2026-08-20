# Vera v3 — Single Source of Truth

> Canonical doc. CLAUDE.md and docs/ARCHITECTURE.md are stubs.
> If you change behavior, change this first.

---

## 1. Vision

Vera is a **second self** — not a chatbot, not a menu, not a triage queue.
She holds Dima's entire knowable context (events, people, projects, goals,
values, voice) in a single graph and makes decisions against the **whole
picture**, not the latest event in isolation.

### Hard rules

1. **Everything Vera knows lives in the graph.** No config files of
   preferences, no scattered tables of rules. If it influences a
   decision, it's a node.
2. **No manual thresholds.** Confidence is alignment with the graph —
   how well a candidate action matches values, goals, past patterns.
3. **One trigger → one decision → graph updated.** Re-processing the
   same event is a bug. Decisions feed back as edges, strengthening or
   weakening future ones.
4. **Source-agnostic.** Every input (Gmail, Telegram, bank, Instagram,
   anything next) implements the same two-method contract; no per-source
   custom branching in the core.
5. **Tool-first.** Vera composes tool calls; she does not contain
   hardcoded business logic.
6. **Owner-only authority.** `OWNER_TELEGRAM_ID` is the single
   privileged identity.

---

## 2. Live infra

| Item | Value |
|---|---|
| Live URL | https://dima.veranda.my |
| Server | Hetzner VPS, SSH alias `hetzner-root` (port 9617) |
| Project dir | `/var/www/vera` |
| SQL | SQLite (WAL) at `/data/vera.db` — minimal, only stateful infra |
| Graph | Neo4j Aura Free → self-host Community when capacity needed |
| Owner Telegram ID | `169510539` |
| Bot | `@Dimondra_Ai_Bot` |
| Forum group for triage | `-1003979512448` («Вера бот»), topics-mode |

---

## 3. The single graph — three layers

```
┌─ L1: Reality (events & entities) ──────────────────────┐
│  Event nodes — every email, message, transaction, etc.  │
│  Person, Project, Domain, Account, Topic, Folder, Chat  │
│  Cheap deterministic edges (regex-extracted) + LLM-     │
│  extracted edges (via Graphiti, in background queue)    │
└─────────────────────────────────────────────────────────┘
┌─ L2: Patterns (learned, not configured) ────────────────┐
│  Pattern nodes — recurring (trigger, action) pairs       │
│  with observation_count, your_correction_count, weight   │
│  Built incrementally from L1 + Dima's decisions          │
└─────────────────────────────────────────────────────────┘
┌─ L3: Identity (who Dima is) ────────────────────────────┐
│  Goal nodes — current week / quarter / year targets      │
│  Value nodes — principles ('respond to customers <4h')   │
│  NoGo nodes — hard prohibitions ('never auto-send $')    │
│  Style nodes — per-relationship tone & voice profile     │
│  Identity nodes — roles, contexts ('CTO Veranda')        │
└─────────────────────────────────────────────────────────┘
```

All three layers live in the same Neo4j. Vera reads from L1+L2+L3 on
every decision. Writes happen continuously: events flow into L1, edges
emerge into L2, conversations with Dima update L3.

---

## 4. Decision flow (the only flow)

```
New event arrives via /event
  ↓
brain.ingest:
  - save Event row in SQL (dedup by source_event_id)
  - cheap_extract → write deterministic edges to graph
  - enqueue deep_extract job (LLM entities, ~30s budget)
  ↓
decide.dispatch:
  - graph query: who+what+when+where+related-to (L1)
  - + pattern matches for this signature (L2)
  - + active goals, applicable values, relevant style (L3)
  ↓
decide.scoring:
  Each candidate action scored on:
    - value alignment (does this match what Dima cares about?)
    - goal contribution (does this help an active goal?)
    - pattern match (have I done this N times before?)
    - NoGo violation (hard block)
    - reversibility (auto only if low-risk)
  → alignment score 0..10
  ↓
Action selection:
  ≥ 7    → auto-execute, post-fact card with [✋ Откати] [❓ Почему]
  3 – 6  → propose with reasoning, card with buttons + Свой ответ
  < 3    → ask plain: «впервые такое: как поступить?»
  ↓
Result is recorded as graph edge:
  Event —[DECIDED_BY {action, score, reversed?}]→ Decision —[REINFORCES]→ Pattern
```

No `auto_threshold` setting. The 7/3 split is itself a Value node
("Vera's autonomy threshold") that Dima can override in conversation:
*«будь смелее — 6 уже делай»* → Value node updated.

---

## 5. Learning loops (no per-event configuration)

| Signal | What graph operation |
|---|---|
| Dima taps action button | Pattern weight += 1, strengthen edge |
| Dima taps ✋ Откати | Pattern weight −= 2, write Correction node |
| Dima writes "no — should be X" | brain.editor LLM-parses → drop old Pattern, create new + Correction |
| Dima writes "always X for Y" | Value node created with high weight |
| Vera notices "you did X 5× — should I learn this?" | If Dima says yes → Pattern promoted to Value |
| Weekly conversation about focus | Goal nodes (re)created with deadline + metric |
| Daily Vera reads sent messages | Style nodes per relationship updated |

No `Trigger` table. No `DecisionReplay` table. No `preferences` for
behavior. All learning lives in graph nodes that Vera both reads and
writes.

---

## 6. Standard source contract

Every input source (existing or future) implements four methods. The first
two are mandatory; `sync_directory` and `tools` are opt-in but recommended.

```python
class Source(ABC):
    async def poll(self) -> AsyncIterator[EventEnvelope]:
        """Yield events newer than last_polled_at."""

    async def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        """Yield events from `since` to now, oldest first."""

    async def sync_directory(self) -> DirectoryDelta:
        """Upsert Entity / Membership / Relationship rows for this source.
        Examples: TG group participants, gmail Contacts, IG followers.
        Default: no-op."""

    def tools(self) -> list[Tool]:
        """Live tools the agent loop can call (look up, search, read).
        Examples: telegram.get_participants, gmail.search, ig.profile_info.
        Default: []."""
```

`EventEnvelope` is a normalised dict with:
```
{
  source: str,               # 'gmail' | 'telegram' | 'instagram' | …
  source_event_id: str,      # stable, for dedup
  account: str | None,
  occurred_at: datetime,
  content_text: str,         # plain text + OCR'd attachments
  attachments: [{kind, sha, ocr_text, ...}],
  entity_hints: [{type, identifier, name, ...}],
  metadata: {...},           # rich, source-specific
}
```

Adding a new source = implement the class + register. The backfill button
in the dashboard, the poll loop, the brain ingest — all already work for
it. No core changes.

---

## 7. Bootstrap (Phase 0 → Phase 5)

| Phase | Duration | What Vera can do at the end |
|---|---|---|
| **0 — Foundation** | 1 week | Graph populated via 6-month backfill of 3 mailboxes + 4 TG categories. Cheap edges built at ingest. Standard source contract live. Old `triage/persona/Trigger/DecisionReplay` removed. |
| **1 — Patterns** | 1 week | L2 Pattern nodes inferred. Replay/confidence via graph traversal, not SQL. Alignment scoring replaces `auto_threshold`. |
| **2 — Values & Goals** | 1 week | Weekly Goal conversation. L3 Goal/Value/NoGo nodes. Brain Editor agent parses Dima's text into graph operations. |
| **3 — Voice** | 1 week | Daily scan of sent. Style nodes per relationship. Outgoing messages style-filtered → sound like Dima. |
| **4 — Proactive** | 1 week | Daily synthesis topic: anomalies vs patterns + goal-progress + suggestions. Vera initiates, not waits. |
| **5 — Polish** | 1 week | Dashboard rebuilt as pure observability. All legacy paths removed. Stress test. |

---

## 8. What we keep from v2

- Gmail OAuth flow + `vera_shared/tokens` encryption
- Telethon userbot session
- HTTP+MCP tool registry (`app/orchestrator/tool_router.py`)
- Topics-mode UX in forum chat
- `app/self_extend/*` (autonomous MCP discovery & install)
- `app/bot/{callbacks, handler, sender, progress}` (minor edits)
- Deploy pipeline (`scripts/deploy.sh` + `.github/workflows/deploy.yml`)
- Security: CSRF, owner-only routes, destructive-args resolver,
  AUTO_SAFE_TOOLS whitelist

## 9. What we remove

- `app/triage/*` → replaced by `app/brain` + `app/decide`
- `app/persona/*` → replaced by `app/brain/identity`
- `app/research/*` → merged into `app/brain/import`
- `app/admin/*` → no manual triage replay anymore
- Tables: `triggers`, `decision_replay` (data migrates to graph)
- `preferences` keys for behaviour (auto_threshold, auto_min_repeats,
  delete_card_after_decision, close_topic_on_decision,
  delete_topic_on_decision, execution_recap_in_dm) — become Value nodes
- Remaining `preferences` keys: `forum_chat_id`, `use_topics` (one-shot
  UX placement, not behaviour)

## 10. Code layout (post-Phase 5)

```
vera-core/app/
├── brain/
│   ├── ingest.py        # event → graph (cheap + queued deep)
│   ├── identity.py      # Goal/Value/NoGo/Style/Identity nodes
│   ├── patterns.py      # Pattern node mining + maintenance
│   ├── editor.py        # text → graph ops via LLM
│   ├── synth.py         # daily proactive synthesis
│   ├── voice.py         # outgoing style filter
│   └── import_.py       # bulk imports (Perplexity etc.)
├── decide/
│   ├── dispatch.py      # event → query → score → act
│   ├── scoring.py       # alignment-based confidence
│   └── explain.py       # rationale for UX ("why")
├── sources/
│   ├── base.py          # Source ABC, EventEnvelope
│   ├── gmail.py
│   ├── telegram.py
│   └── registry.py      # discovery + jobs
├── jobs/
│   ├── runner.py        # backfill + ingest queue worker
│   └── models.py        # BackfillJob, IngestJob
├── bot/                 # callbacks, handler, sender (unchanged)
├── orchestrator/        # tool_router, loop (unchanged)
├── mcp/                 # MCP manager (unchanged)
├── self_extend/         # tool discovery & install (unchanged)
├── events/routes.py     # /event ingest, dedup
└── dashboard/           # rebuilt — observability only
```

## 11. Security invariants (carried over)

| Boundary | Enforcement |
|---|---|
| `/internal/*` | X-Internal-Secret + host allowlist + Depends(require_owner) for GET |
| `/api/*` mutating | Depends(require_owner) + CSRF X-CSRF header |
| Owner cookie | session_secret HMAC, samesite=strict, 7d TTL |
| Tokens at rest | AES-CTR + HMAC |
| Destructive tool args (`*send*`, `*reply*`, `*delete*`) | re-derived server-side, LLM-chosen recipient overridden + logged |
| Auto-execution | only tools in AUTO_SAFE_TOOLS may auto-fire |
| Triage callback | owner_id + chat_id check |

## 12. Deploy

```bash
git push origin master   # GH Action → scripts/deploy.sh
# or
ssh hetzner-root "/var/www/vera/scripts/deploy.sh"
```

`scripts/deploy.sh` does: lock → git pull → build → up -d → smoke
(dashboard + per-service health) → pytest → cleanup. On failure GH
Action posts to Telegram and runs ROLLBACK_SHA reset.

## 13. Migration log

- **2026-06-09 (this commit)**: v3 substrate consolidated. Graph layer is
  materialized inside Postgres (not Neo4j yet) via `entities` /
  `entity_aliases` / `memberships` / `relationships` / `identity_nodes` /
  `patterns` tables behind a `graph_repo` API — Neo4j swap is a one-file
  change later. Phase 3 (Voice / Style nodes per relationship)
  **promoted ahead of Phase 2** at owner's request: Vera must learn Dima's
  per-recipient writing style (formality вы/ты, length, emoji rate,
  opening/closing patterns, vocabulary signatures, code-switching ratio,
  sample messages) and draft outgoing messages in that voice via
  `tools.style.draft(recipient, intent)`. Tool layer formalized:
  `shared/vera_shared/tools/{base,registry,http_client,memory,search,style}.py`
  with one registry; every ingestor exposes `/tools/*`. New §19 below.

- **2026-06-01**: Brain auto-feedback loop killed — `vera-monitor` now
  ignores Graphiti ingest errors (Gemini/Voyage rate-limits). DRY pass:
  shared `vera_shared.internal_auth.require_internal` and
  `vera_shared.llm.json_parse.{strip_fence,safe_parse}` replace 6
  duplicated copies. Security: `/tool/{name}` now requires X-Internal-Secret.
- **2026-05-30**: Gemini 2.5 → 3.5 Flash (7× faster, +452 Elo on agentic).
  Graphiti key rotation fixed (429 cooldown 1h instead of falling through
  to DeepSeek which rejected `response_format: json_schema`).
  vera-deploy script: auto-prunes images + builder cache.
  vera-monitor cron checks 12 health dimensions every 5 min.
- **2026-05-22**: v3 spec adopted — single graph, no per-event config,
  6-week phased rebuild. Triage/persona/Trigger/DecisionReplay deprecated.
- 2026-05-21 → 2026-05-22: v2 (current production) — topics, replay
  table, threshold-based auto, scattered prefs. Now legacy.

## 14. Database schema (SQLite, `/data/vera.db`)

Schema is in `shared/vera_shared/db/models.py`. Run migrations on startup
via `vera_shared.db.migrations.run_migrations`.

**Core tables:**

| Table | Purpose | Key columns |
|---|---|---|
| `tokens` | API keys (encrypted at rest) for all providers | `provider`, `label`, `token` (encrypted), `capabilities`, `daily_used`, `cooldown_until` |
| `events` | Every observed signal: gmail/telegram/monitor/deploy | `source`, `source_event_id` (unique), `content_text`, `entity_hints`, `triage_status`, `triage_result`, `graphiti_episode_uuid` |
| `gmail_accounts` | OAuth-connected mailboxes | `email`, `refresh_token_enc`, `access_token_enc`, `is_active`, `last_polled_at` |
| `ig_accounts` | Instagram sessions | `username`, `access_token_enc` (encrypted instagrapi session JSON), `status` |
| `agents` | HTTP MCP agents registered with vera-core | `id`, `name`, `http_url`, `capabilities`, `last_heartbeat` |
| `backfill_jobs` | Async backfill queue | `source_name`, `since`, `status`, `count_processed` |
| `mcp_servers` / `mcp_proposals` | Self-extension (proposed/installed MCP) | see SELF_EXTENSION.md |

**Graph (Neo4j Aura, free tier, 200K nodes / 400K rel limit):**

| Node label | Meaning |
|---|---|
| `Episodic` | Raw event stored verbatim (1 per event) |
| `Entity` | Person / chat / account / project — auto-extracted by Graphiti |
| `Community` | Cluster of related entities |
| `Saga` | Cross-event narrative (currently unused) |

Relationships: `MENTIONS` (episodic → entity), `RELATES_TO` (entity ↔ entity,
with `fact` text), `HAS_EPISODE` (community ↔ episodic), `NEXT_EPISODE`
(temporal chain).

## 15. API reference

All routes require `Depends(require_owner)` (HMAC session cookie + CSRF on
mutating methods) unless noted.

### Owner-facing (dashboard)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/observability` | Health snapshot (DB, queue, Graphiti, tokens) |
| GET/POST | `/api/gmail/oauth/start` `/callback` | OAuth flow for Gmail accounts |
| GET/POST/DELETE | `/api/instagram/accounts` | IG session CRUD via instagrapi |
| GET/POST/PATCH/DELETE | `/api/instagram/autoreplies` | DM auto-reply rules |
| POST | `/api/self_extend/discover` `/uninstall/{name}` | Manual MCP install/remove |
| POST | `/api/sources/*` | Source registry CRUD |

### Service-to-service (X-Internal-Secret required)

| Method | Path | Purpose |
|---|---|---|
| POST | `/event` | Submit a signal from any source |
| POST | `/internal/agents/register` | Agent heartbeat |
| POST | `/internal/llm/chat` | LLM proxy for other vera-* containers |
| GET | `/internal/coder/github-token` | PAT for vera-coder |
| POST | `/internal/coder/notify` | PR-ready notification → DM Dima |
| POST | `/tool/{name}` | Invoke a vera-core self-tool (deploy, send_telegram, etc.) |

### Public (no auth)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe |
| POST | `/api/tg_login` | Telegram-login widget callback (validates TG hash) |

## 16. Deployment runbook

**Standard deploy:**
```
git push origin master
ssh hetzner-root vera-deploy [vera-core|vera-gmail|vera-telegram|vera-coder]
```

The `vera-deploy` script (`/usr/local/bin/vera-deploy`) does: `git pull →
docker compose build --no-cache <svc> → up -d → docker image prune -f →
docker builder prune --keep-storage=1gb -f → POST /event {source=deploy}`.

**Health monitor:** `/usr/local/bin/vera-monitor` runs every 5 min via
root cron. Checks 12 dimensions (containers, disk, memory, /health, error
rate, Gmail OAuth expiry, triage backlog, Gemini quota, Voyage quota, SSL
cert). Alerts go to Vera as `source=monitor` events; she surfaces them in
Telegram. Throttle: 1 alert per key per 30 min.

**Backup/restore SQLite:**
- Backup: `ssh hetzner-root "sqlite3 /var/www/vera/data/vera.db .backup /tmp/vera-$(date +%F).db"`
- Restore: stop `vera-core`, `cp` over `/var/www/vera/data/vera.db`, start.

**Neo4j Aura:** managed by Neo4j Inc. Connection string in `.env`
(`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`). No local backups — the
free tier keeps 7-day point-in-time recovery in the Aura console.

## 17. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Episode add failed: No gemini tokens` | All Gemini keys in cooldown (free tier RPM exhausted) | Wait 60s, or check that `gemini/demoniwwwe` paid key billing is active. Verify `tokens.cooldown_until` in DB. |
| `Episode add failed: ... reduced rate limits of 3 RPM and 10K TPM` (Voyage) | Voyage account has no payment method | Visit dashboard.voyageai.com → Settings → Payment → add card. 200M free tokens stay; payment method unlocks Tier 1. |
| Gmail poller `invalid_grant` for all accounts | Google revoked refresh tokens (idle > 7 days while app is in "Testing") | Re-authorize via `/api/gmail/oauth/start` per account. Long-term fix: publish OAuth app to "Production" in Google Cloud Console. |
| Disk fills up | Old Docker images, build cache | `vera-deploy` auto-prunes. Manual: `docker image prune -af && docker builder prune --keep-storage=1gb -f`. |
| `/tool/{name}` returns 404 | Tool name typo or HANDLERS not loaded | Check `app/system/tools.py` `HANDLERS` dict. Restart vera-core. |
| `/tool/{name}` returns 401 | Caller didn't send `X-Internal-Secret` header | Add the header. INTERNAL_SECRET env var on caller must match server. |
| Triage produces nothing for hours | All LLM providers down / no active tokens | Check `/api/observability` for token table. Add a fresh Gemini free key via dashboard Settings. |
| brain empty but events arrive | Graphiti ingest fails silently | Check vera-core logs for `Episode add failed`. Likely Voyage or Gemini quota; see rows above. |
| Container in `Restarting` loop | Bad migration or corrupt env | `docker compose -f /var/www/vera/docker-compose.yml logs --tail=200 <svc>` and read the traceback. |

## 18. Local development

Prerequisites: Python 3.12, docker (for full stack), or just sqlite for tests.

```bash
# Run unit tests
cd vera-core
PYTHONPATH=../shared pytest -x

# Run a single service against test SQLite
DB_PATH=/tmp/vera-test.db SESSION_SECRET=dev INTERNAL_SECRET=dev \
  uvicorn app.main:app --reload --port 8001
```

Migrations apply automatically on startup. Test fixtures live in
`vera-core/tests/conftest.py` — they isolate a temp SQLite per session.

---

## 19. Style profiling — how Vera sounds like Dima

Phase 3 promoted ahead. Vera holds a **per-relationship Style profile** in
L3 (`identity_nodes` where `type='style'`) and uses it whenever she drafts
a message on Dima's behalf.

### Profile shape

```jsonc
{
  "speaker": "dima",
  "listener_entity_id": 4711,        // resolved Person/Chat in entities
  "listener_label": "Маша",
  "based_on_n_messages": 234,
  "formality": "ty",                 // 'vy' | 'ty' | 'mixed'
  "avg_length_chars": 86,
  "avg_sentences": 1.4,
  "emoji_per_msg": 0.6,
  "frequent_emoji": ["🙏","🤙","😂"],
  "openings": ["Слушай,", "Привет!", "Окей"],
  "closings": ["Обнимаю", "Спасибо", "—"],
  "vocabulary_signatures": ["надо","короче","шарю","заебись"],
  "code_switching": {"ru": 0.78, "en": 0.18, "uk": 0.04},
  "median_response_latency_min": 9,
  "sample_messages": [
    {"event_id": 22310, "text": "ок, через час буду"},
    {"event_id": 22871, "text": "слушай, перенесём на среду?"},
    ...
  ],
  "updated_at": "2026-06-09T03:00:00Z",
  "confidence": 0.83
}
```

### Generation pipeline

```
brain-voice service (nightly cron, 03:00 UTC):
  1. SELECT events WHERE direction='sent' AND occurred_at > now()-90d
  2. group by resolved_listener_entity_id
  3. for each (Dima → Listener):
       - compute stats (formality, length, emoji, code-switch)
       - extract n-gram vocabulary signatures
       - pick 10 representative samples (medoid by embedding)
       - UPSERT identity_node(type='style', payload=profile)
  4. global Dima style fallback (any-listener) also stored
```

### Tool exposure

```
tools.style.get(listener)              → StyleProfile | None
tools.style.draft(listener, intent,    → {text, model, profile_used}
                  context, length_hint)
tools.style.list_profiles()            → [(listener, n_messages, updated_at)]
```

`draft()` builds the LLM prompt:
```
You are Dima writing to {listener_label}. Match this voice exactly:
{compact profile}
Examples of how Dima writes to them:
{5 sample_messages verbatim}
Intent: {intent}
Context: {context}
Write the message. Plain text. {length_hint or 'aim for avg_length_chars ±30%'}.
```

### Hard rules

- Style is never `'auto-send'`. Drafts always require explicit owner ✅
  before the corresponding send tool fires (Telegram, Gmail, IG).
- Style is **observed, not configured.** Dima can override a profile
  field in chat ("с Машей я теперь на вы"); the editor agent writes a
  Correction edge and the next nightly rebuild respects it.
- Style profiles are derivable. They are NOT a source of truth — they
  are a cache over `events.direction='sent'`. Drop the row, the next
  voice run rebuilds it.

---

## Appendix A — Why no manual thresholds

Past lesson: every numeric knob (`auto_threshold`, `auto_min_repeats`,
trigger predicates) became an out-of-date config that conflicted with
the graph state. Single rule going forward: if a setting influences
decisions, it's a node. If it's just UX placement (DM vs group), it's
a pref. Two-line rule, no middle ground.

## Appendix B — Why one trigger = one triage (enforced at the code level)

`_run_triage` checks `event.triage_status` and returns early if not
'pending'. `save_event` returns `(event, is_new)` and `/event` only
schedules ingestion when new. The flood-of-cards bug (event #477 got
20+ cards) was caused by violating this in two places at once.

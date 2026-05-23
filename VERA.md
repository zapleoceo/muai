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

Every input source (existing or future) implements two methods:

```python
class Source(ABC):
    async def poll(self) -> AsyncIterator[EventEnvelope]:
        """Yield events newer than last_polled_at."""

    async def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        """Yield events from `since` to now, oldest first."""
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

- **2026-05-22**: v3 spec adopted — single graph, no per-event config,
  6-week phased rebuild. Triage/persona/Trigger/DecisionReplay deprecated.
- 2026-05-21 → 2026-05-22: v2 (current production) — topics, replay
  table, threshold-based auto, scattered prefs. Now legacy.

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

# Domain model

Authoritative: `vera3/shared/vera_shared/db/models.py` +
`vera3/shared/vera_shared/db/models_*.py` + `vera3/infra/migrations/`.

## Core tables

### `events`

Append-only signal log. Every observation enters here.

| Column | Notes |
|---|---|
| `id` | BIGSERIAL |
| `source` | `telegram` / `gmail` / `instagram` / `vera_chat` / `vera_memory` / `perplexity` / `monitor` |
| `source_event_id` | Stable per-source identifier; uniqueness key |
| `account` | Email / username / handle that received or sent the event |
| `category` | source-specific (e.g. `user`, `channel`, `email`) |
| `content_text` | Plain text body. Media → placeholder strings. |
| `occurred_at` | When the event happened (sender clock) |
| `received_at` | When Vera saw it |
| `triage_status` | `pending` / `processing` / `done` / `error` / `dead` (retries exhausted) / `superseded` (semantic dedup, see `gateway/claude.py`) / `media_pending` (photo/voice waiting on broker vision/transcription) |
| `triage_metadata` | JSONB: importance, topics, people, signals, needs_action |
| `importance` | 0-100 (denormalized from triage_metadata for fast filters) |
| `metadata` | JSONB, source-specific (chat_id, sender_username, direction, …) |

Embedding is **not** a column on `events` (migration 011 moved it out —
see `event_embeddings` below). Any ORM code doing `select(EventRow)` that
still references `embedding_voyage_3` will crash with `UndefinedColumnError`
— this happened once already after the split; the mapped_column was
removed from `EventRow` and only a comment marks where it used to live.

For `source='telegram'`, `metadata.chat_kind` is `private` / `group` /
`channel` / `other` — the single field for that distinction. Computed by
`ingestor_telegram.userbot.classify_chat_kind()`. Supergroups are a
Telethon `Channel` object just like broadcast channels — only
`chat.megagroup` tells them apart, so `chat_kind` (not the legacy
`is_channel`/`is_group`/`is_supergroup` fields, kept for back-compat but
don't rely on them) is the correct signal for "is this a real group chat."
`brain_triage.worker.chat_kind()` recomputes the same classification from
older `chat_type`/`is_supergroup` fields for events written before this
field existed — no backfill/migration needed.

### `event_embeddings` (migration 011)

`EventEmbeddingRow` — Voyage embedding, split out of `events` into its own
narrow table: `event_id` (PK, FK → `events.id` ON DELETE CASCADE),
`embedding` (JSONB, 1024-dim vector), `created_at`. Reason: embeddings
inline made `events` ~3.9GB, so every `COUNT`/`GROUP BY` scanned the whole
table. Search/dedup/triage all read/write this table via `LEFT JOIN
event_embeddings ee ON ee.event_id = events.id` — never the old column.

### `usage_log`

LLM call accounting. Mirror of the broker's view for dashboard/analytics.
Vera has **no `tokens` table** — dropped in migration 008. All provider
keys live in AIbroker; Vera holds none. See `llm-broker.md`.

`request_id` and `key_label` (migration 012) — captured from the broker's
chat (`/v1/jobs` submit+poll since the 2026-07 async migration — see
`llm-broker.md`) and `/v1/embed` responses (`request_id` arrives as an
int, cast to `str` before insert — the column is `VARCHAR`). Joined into the
dashboard's `/events` (renamed "log") page via a `LEFT JOIN LATERAL` on
the latest row per `event_id`, showing which model/tokens/cost produced
each event's triage. Batch-triaged group messages share ONE `usage_log`
row (the broker call covers N events) — the log page shows those as
"в пачке ✓" rather than a blank, since the event genuinely was processed,
just not with its own billed row.

### Source-specific config

| Table | Purpose |
|---|---|
| `gmail_accounts` | OAuth state per mailbox. `refresh_token_enc`, `last_polled_at`, `is_active`. |
| `telegram_sessions` | Telethon MTProto session (StringSession), encrypted. |
| `instagram_sessions` | instagrapi sessionid + device fingerprint, encrypted. |

### `project_membership` (migration 010)

Deterministic source of truth for `events.project`, replacing pure
LLM-guessing for `itstep`/`veranda`. Populated by
`ingestor-telegram/sync_projects.py` (manual/cron run) from:

| `kind` | `key` | Rule (see `vera_shared/projects/rules.py`) |
|---|---|---|
| `chat` | canonical chat_id (supergroup `-100` prefix stripped) | Telegram folder "ItStep" → `itstep`; chat title contains "veranda"/"веранда" → `veranda` |
| `account` | ILIKE pattern | Gmail account `%itstep.org%` → `itstep` |
| `person` | Telegram sender_id | Derived: anyone who posted in a project chat (excluding owner) |

PK `(project, kind, key)` — a person/chat can belong to only one row per
project (but the same key can appear under multiple projects if someone
is in chats for two different projects).

`brain_triage/worker.py::process_pending()` applies this after every
triage batch: chat/account membership overrides the LLM's `project`
guess, and any LLM-guessed `itstep`/`veranda` on a telegram chat that
ISN'T in `project_membership` gets reset to `other` (closes the loop —
LLM can no longer silently misclassify a chat as itstep/veranda that
membership doesn't recognize).

A fourth override in the same block handles `source='manual'` events
(notes inserted directly via `gateway`'s `/event/manual`, e.g. call
summaries, daily updates): if the event's own `metadata.project_hint`
is `itstep` or `stepan` (Stepan is an IT STEP Jakarta product, not a
separate project), `project` is forced to `itstep` — the note author's
own hint outranks the LLM's text-only guess, same precedence rule as
chat/account membership.

## Substrate (L1/L2/L3 graph)

Materialized in Postgres. Behind `vera_shared/graph/repo.py` API so a future
Neo4j swap is a one-file change.

### L1 — Reality

- `entities` — resolved real-world thing (person, group, channel, place, project)
- `entity_aliases` — `(source, identifier) → entity_id` for identity resolution
- `memberships` — "X is in Y" (e.g. user is member of TG group)
- `relationships` — Graphiti-style edges with `predicate`, `fact`, `confidence`.
  `derived_from_event_id` has an FK → `events.id` ON DELETE SET NULL
  (migration 013, added `NOT VALID` — enforces on new/changed rows without
  scanning/locking existing ones). Deleting an event no longer leaves a
  dangling reference in the graph.
- `entity_avatars` (migration 014) — profile-photo blob side-table
  (`EntityAvatarRow`: `entity_id` PK, `image` BYTEA, `mime`, `missing`,
  `fetched_at`). Kept out of the hot `entities` table. Written lazily+throttled
  by the ingestor-telegram avatar backfill; read by the dashboard
  `/entities/{id}/avatar` route (`entity_avatar`), which falls back to an
  `initials_avatar_svg` when no photo is stored. Read/write primitives live in
  `vera_shared/graph/avatars.py` (`get_avatar`, `upsert_avatar`,
  `list_entities_needing_avatar`).

**Dedup review** (`/entities/duplicates`): `find_duplicates_by_name` groups
same-normalized-name entities (mostly *different* people sharing a first name —
low precision), while `find_alias_collisions` groups entities sharing one
lowercased `@username` — the high-precision "real duplicate" signal (a channel
posting under its own handle spawns both a `channel` and a `person` entity for
one handle). Each candidate is shown with its avatar, a `tg_link` back to
Telegram, and a `get_entity_dossiers` context block (dominant project, top
chats, recent message snippets) so the owner can tell WHO an entity is before
merging. It matches a person's messages by the numeric tg_id in
`events.metadata->>'sender_id'` — indexed by migration 015
(`ix_events_tg_sender`, partial on `source='telegram'`) — and is **batched**
(4 set-based queries for all candidates in one session; per-entity fanout of
`get_entity_dossier` would exhaust the dashboard connection pool on a
~200-candidate page).

### L2 — Patterns (reserved for future)

- `patterns` — `(trigger_signature, action_kind, weight)` learned from feedback

### L3 — Identity

- `identity_nodes` with `type ∈ {goal, value, nogo, style, self, preference, fact}`
- Style per relationship: `listener_entity_id` → payload with formality,
  avg length, sample messages

## Migrations

`vera3/infra/migrations/*.sql` — raw SQL, applied by hand:
`docker exec -i vera3-postgres psql -U vera -d vera < migration.sql`. There is
no migration runner and no Alembic — the feature size has not justified the
overhead. `vera_shared/db/migrations.py` does **not** exist; it was only ever
an idea in this doc.

The graph schema is bootstrapped from `infra/sql/graph_substrate.sql`, the
only file under `infra/sql/`. An `init.sql` referenced here until 2026-08-06
never existed.

Numbering is advisory, not enforced: `014` is used twice
(`014_entity_avatars.sql`, `014_events_pending_claim_index.sql`) and `018`/`019`
were never issued. Since nothing records what ran, verify against the database
before assuming a file was applied — e.g. `SELECT to_regclass('public.<object>')`
for the object it creates.


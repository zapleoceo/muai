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
| `triage_status` | `pending` / `processing` / `done` / `error` |
| `triage_metadata` | JSONB: importance, topics, people, signals, needs_action |
| `importance` | 0-100 (denormalized from triage_metadata for fast filters) |
| `embedding_voyage_3` | pgvector 1024-dim |
| `metadata` | JSONB, source-specific (chat_id, sender_username, direction, …) |

### `usage_log`

LLM call accounting. Mirror of the broker's view for dashboard/analytics.
Vera has **no `tokens` table** — dropped in migration 008. All provider
keys live in AIbroker; Vera holds none. See `llm-broker.md`.

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

## Substrate (L1/L2/L3 graph)

Materialized in Postgres. Behind `vera_shared/graph/repo.py` API so a future
Neo4j swap is a one-file change.

### L1 — Reality

- `entities` — resolved real-world thing (person, group, channel, place, project)
- `entity_aliases` — `(source, identifier) → entity_id` for identity resolution
- `memberships` — "X is in Y" (e.g. user is member of TG group)
- `relationships` — Graphiti-style edges with `predicate`, `fact`, `confidence`

### L2 — Patterns (reserved for future)

- `patterns` — `(trigger_signature, action_kind, weight)` learned from feedback

### L3 — Identity

- `identity_nodes` with `type ∈ {goal, value, nogo, style, self, preference, fact}`
- Style per relationship: `listener_entity_id` → payload with formality,
  avg length, sample messages

## Migrations

`vera3/infra/migrations/*.sql` are timestamped raw SQL files. Run manually
via `docker exec vera3-postgres psql ... < migration.sql`. The init script
in `infra/sql/init.sql` only runs on first Postgres boot.

We don't have Alembic yet — feature size hasn't justified the overhead.
When it does, plug in `vera_shared/db/migrations.py`.

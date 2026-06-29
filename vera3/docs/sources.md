# Sources (ingestors)

Each source has its own container and writes to the same `events` table
via `gateway /event/<source>` with `X-Internal-Secret`.

## telegram

- Container: `vera3-ingestor-telegram`
- Mechanism: Telethon userbot (MTProto), single StringSession for `@zapleosoft` stored encrypted in `telegram_sessions`.
- What it captures: every incoming + outgoing message in every dialog (DM, groups, supergroups, channels where the user is a member).
- Format: every message saved. Pure-text — as-is. Media — placeholder `[photo]` / `[voice:12s]` / `[video]` / `[sticker:😀]` etc. + `metadata.media_kind` + `media_meta`. Photo/voice/audio get `triage_status='media_pending'` so the media-worker (PR2) can download and run vision/whisper, then move to normal triage.
- Tools server: same container exposes `:8000/tools/*` for the agent loop (`list_dialogs`, `get_participants`, `get_chat_info`, etc.).
- History backfill: a one-shot queue walked every dialog back to 2025-06-01 (6067 dialogs, ~323k messages), completed 2026-06-29. The `backfill_jobs` queue, its worker, the seeder, and the dashboard `/backfill` page were retired afterwards (migration 009). Live ingestion covers everything since; to backfill again, re-apply migration 007 and restore the worker from git history.

## gmail

- Container: `vera3-ingestor-gmail`
- Mechanism: OAuth refresh + Gmail API polling every 5 min per account.
- Accounts: 3 (`demoniwwwe@gmail.com`, `zaporozec_d@itstep.org`, `zapleosoft@gmail.com`).
- Critical caveat: tokens get revoked by Google if the OAuth app sits in
  "Testing" mode for >7 days idle. See `security.md` for re-auth flow.

## instagram

- Container: `vera3-ingestor-instagram`
- Mechanism: `instagrapi` (unofficial mobile API).
- Auth: sessionid imported from owner's Chrome cookies (see `scripts/auth_ig_sessionid.py`).
- Caveat: Instagram aggressively logs out idle sessions. Re-auth ~weekly via the same script.
- Tool: `[shared post]` / `[reel]` / `[voice]` / `[media]` placeholders for non-text.

## vera_chat

- Not an external source. Bot writes user prompts AND Vera's replies here.
- Used by `brain-search` to retrieve the last N pairs as conversation context.

## vera_memory

- Not an external source. The agent loop's `memory.remember(fact)` tool writes here when it derives a non-obvious truth.

## perplexity (one-shot)

- `scripts/import_perplexity.py` — imports Perplexity MD exports as events.
- Source name = `perplexity`. Run once when there's a new bundle.

## Authorship contract (telegram / gmail / instagram)

Every event from a conversational source MUST encode author unambiguously:

- `content_text` first line: `Author: <label> [<self|counterparty>]`
- `metadata.author_role` = `self` | `counterparty`
- `metadata.author_label` = `Я` (for self) | `@username` | from-address | fallback chat_title

This exists because `chat_title` in a personal chat = the *other* party, but a
`direction=sent` message in that chat is authored by the owner, not by the
counterparty. Consumers (the agent loop, dashboards, ad-hoc SQL) must look at
`author_role`, never at `chat_title`, to decide who wrote a message.

Migration that backfills both fields + the content_text prefix:
`infra/migrations/005_author_role.sql` — idempotent (guarded by
`content_text NOT LIKE 'Author:%'`).

## Adding a new source

1. New service under `services/ingestor-<name>/`.
2. Implement `vera_shared.sources.base.Source` ABC (poll + backfill).
3. POST normalized events to `gateway /event/<name>` with internal secret.
4. Write `author_role` + `author_label` into metadata and prepend `Author:` to content_text (see authorship contract above).
5. Update [domain-model.md](./domain-model.md) if you add new metadata fields.
6. Update this file with the source's quirks.

# Sources (ingestors)

Each source has its own container and writes to the same `events` table
via `gateway /event/<source>` with `X-Internal-Secret`.

## telegram

- Container: `vera3-ingestor-telegram`
- Mechanism: Telethon userbot (MTProto), single StringSession for `@zapleosoft` stored encrypted in `telegram_sessions`.
- What it captures: every incoming + outgoing message in every dialog (DM, groups, supergroups, channels where the user is a member).
- Format: every message saved. Pure-text — as-is. Media — placeholder `[photo]` / `[voice:12s]` / `[video]` / `[sticker:😀]` etc. + `metadata.media_kind` + `media_meta`. Photo/voice/audio get `triage_status='media_pending'` so the media-worker (PR2) can download and run vision/whisper, then move to normal triage.
- Tools server: same container exposes `:8000/tools/*` for the agent loop (`list_dialogs`, `get_participants`, `get_chat_info`, etc.).
- Avatar backfill: `avatar_backfill.run_avatar_backfill(client)` runs as a slow background task on the same session — downloads entity profile photos (highest-degree first) into `entity_avatars` for the graph/dedup UI. Anti-ban: ~1 photo / `AVATAR_FETCH_INTERVAL_S` (default 4s), backs off on FloodWait, pauses with the owner's backfill switch (`is_backfill_paused`), and marks unresolvable/photoless entities `missing` so they're never retried. Env knobs: `AVATAR_BACKFILL_ENABLED`, `AVATAR_BATCH`, `AVATAR_FETCH_INTERVAL_S`, `AVATAR_IDLE_SLEEP_S`.
- History backfill: a one-shot queue walked every dialog back to 2025-06-01 (6067 dialogs, ~323k messages), completed 2026-06-29. The `backfill_jobs` queue, its worker, the seeder, and the dashboard `/backfill` page were retired afterwards (migration 009). Live ingestion covers everything since; to backfill again, re-apply migration 007 and restore the worker from git history.

## gmail

- Container: `vera3-ingestor-gmail`
- Mechanism: OAuth refresh + Gmail API polling every 5 min per account.
- Identity graph: собеседник каждого нового письма становится person-сущностью
  с alias `(gmail, email)` — см. `docs/identity.md`. Кросс-канальное слияние
  с telegram-сущностями — через LLM-предложения Веры на `/entities/duplicates`.
- Accounts: 3 (`demoniwwwe@gmail.com`, `zaporozec_d@itstep.org`, `zapleosoft@gmail.com`).
- Critical caveat: tokens get revoked by Google if the OAuth app sits in
  "Testing" mode for >7 days idle. See `security.md` for re-auth flow.
- Backlog-safe polling (2026-07-17): `poller.fetch_message_ids()` lists
  the whole backlog id-only (cheap, up to 10×`GMAIL_MAX_PER_RUN`),
  `poller.filter_new_ids()` drops ids already in `events` BEFORE the
  expensive per-message GET, then `poller.fetch_full_messages()` fetches
  at most `GMAIL_MAX_PER_RUN` (default 500) new ones. If more new mail
  remains, the run is *truncated*: `last_polled_at` is NOT advanced, so
  the next run re-lists from the same date and picks up the tail. The old
  code fetched the 500 NEWEST, jumped the cursor to today, and silently
  lost the older tail forever (`after:` is date-granular).
  `poller.fetch_messages()` remains as a thin list+get composition.

## instagram

- Container: `vera3-ingestor-instagram`
- Mechanism: `instagrapi` (unofficial mobile API).
- Auth: **dashboard admin UI** — `/sources` → "🔑 Подключить Instagram"
  button → `dashboard/instagram_login.py` (`instagram_start_form`,
  `instagram_start`, `instagram_verify`). Username/password login with
  inline 2FA/challenge-code handling (same flow shape as Stepan2's
  `ig_client.py`, no proxy — this is a personal account, not multi-tenant).
  On success the session (`cl.get_settings()`) is encrypted and upserted
  into `instagram_sessions`, `is_active=True`. The password is held only
  in an in-memory flow dict for the duration of the 2FA round-trip (TTL
  600s) — never logged, never persisted in plaintext.
- If there's no active session, `load_client()` raises `RuntimeError`;
  `main()` catches it and waits (re-checks every 10 min) instead of
  crashing — `restart:unless-stopped` previously crash-looped the
  container forever (RestartCount climbing unbounded) whenever the
  session was inactive, which is the common case between logins.
- `SessionDead` (raised from `poll_once()` on `LoginRequired`/
  `ChallengeRequired` mid-poll) marks the session `is_active=False` and
  exits the poll loop cleanly, instead of retrying against a dead session
  every `IG_POLL_INTERVAL_S`.
- Dedup batches one query per thread (`_existing_sids`) instead of one
  `SELECT` per message. `IG_MSGS_PER_THREAD` default raised 20→50 — a
  20-message window could miss messages in an active thread between polls.
- Tool: `[shared post]` / `[reel]` / `[voice]` / `[media]` placeholders for non-text.

## vera_chat

- Not an external source. Bot writes user prompts AND Vera's replies here.
- Used by `brain-search` to retrieve the last N pairs as conversation context.

## vera_memory

- Not an external source. The agent loop's `memory.remember(fact)` tool writes here when it derives a non-obvious truth.

## perplexity (one-shot)

- `scripts/import_perplexity.py` — imports Perplexity MD exports as events.
- Source name = `perplexity`. Run once when there's a new bundle.

## Ingest denylist (в мозг не пишем)

`vera_shared.ingest_policy` — два уровня, оба применяются в
`userbot.save_message()` ДО записи события (в отличие от `media_policy`,
где событие сохраняется, но не распознаётся картинка):

- `is_ignored_sender(username)` — по автору сообщения;
- `is_ignored_chat(chat_username, chat_title)` — **весь чат целиком**, обе
  стороны переписки.

Матчинг по username (стабилен, в отличие от имени), регистр и ведущий `@`
не важны; для чата дополнительно по началу названия — у ботов username
есть не всегда.

**Почему нужен уровень чата.** 2026-08-02 в очередь распознавания хлынул
`@leomatchbot` (Дайвінчик, бот знакомств): ~800 анкетных фото в сутки, при
том что vision столько не переваривает в принципе — очередь выросла до 1366.
Чат приватный, поэтому ни правило про вещательные каналы, ни денай-лист
шумных групп его не ловили. Фильтра по отправителю тоже мало: исходящие в
таком чате — 👎-свайпы самого владельца, у них `sender_username` его
собственный. Дима: «весь чат в игнор поставь». Вычищено 4037 событий
(+3259 эмбеддингов каскадом), очередь 1366 → 561.

Сейчас в списке: `verandamybot`. Добавлен 2026-07-31 (Дима: «@VerandamyBot
исключи из мозга вообще») — служебный бот сыпал машинными уведомлениями в
рабочие чаты: 6050 событий («Веранда сотрудники» 5132, «VerandaBot» 736,
«Старшие и отчеты» 182). Для личной памяти это шум: раздувает базу, ломает
поиск, жжёт бюджет триажа, а сами факты есть в системе-источнике. Историю
вычистили (события + эмбеддинги каскадом + сущность #8604 с алиасами и
членствами); 2 сообщения ДРУГИХ авторов, где бот лишь упомянут, сохранены.

Чтобы добавить ещё источник:

- шумит только бот → username в `_IGNORED_SENDER_USERNAMES`, чистка
  `DELETE FROM events WHERE metadata->>'sender_username' ILIKE '<username>'`;
- мусорят обе стороны → `_IGNORED_CHAT_USERNAMES` (или
  `_IGNORED_CHAT_TITLE_PREFIXES`), чистка
  `DELETE FROM events WHERE metadata->>'chat_title' ILIKE '%<название>%'`.

`event_embeddings` уйдут по каскаду, `relationships.derived_from_event_id`
обнулится.

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

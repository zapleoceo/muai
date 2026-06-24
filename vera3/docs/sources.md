# Sources (ingestors)

Each source has its own container and writes to the same `events` table
via `gateway /event/<source>` with `X-Internal-Secret`.

## telegram

- Container: `vera3-ingestor-telegram`
- Mechanism: Telethon userbot (MTProto), single StringSession for `@zapleosoft` stored encrypted in `telegram_sessions`.
- What it captures: every incoming + outgoing message in every dialog (DM, groups, supergroups, channels where the user is a member).
- Format: text only — media gets a `[photo]` / `[voice]` / `[video]` placeholder (TODO: extract via Gemini Vision / Whisper).
- Tools server: same container exposes `:8000/tools/*` for the agent loop (`list_dialogs`, `get_participants`, `get_chat_info`, etc.).

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

## Adding a new source

1. New service under `services/ingestor-<name>/`.
2. Implement `vera_shared.sources.base.Source` ABC (poll + backfill).
3. POST normalized events to `gateway /event/<name>` with internal secret.
4. Update [domain-model.md](./domain-model.md) if you add new metadata fields.
5. Update this file with the source's quirks.

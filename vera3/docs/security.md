# Security

## Owner gate

`OWNER_TELEGRAM_ID=169510539` is the single privileged identity. Every
admin path checks against it:

- Dashboard `/login`: TG widget verifies HMAC; only user_id=OWNER passes.
- Bot DM: `bot.py` rejects any other sender.
- No "additional admin" concept anywhere. Don't add one without revisiting
  the threat model.

## Secrets at rest

| Asset | Storage | Encryption |
|---|---|---|
| Gmail refresh tokens | `gmail_accounts.refresh_token_enc` | Fernet (key from `TOKEN_SECRET`) |
| TG MTProto session | `telegram_sessions.session_string_enc` | Fernet |
| Instagram sessionid | `instagram_sessions.session_json_enc` | Fernet |
| LLM provider tokens | `tokens.token_encrypted` (cold fallback) — primary copy in AIbroker DB | Fernet |
| Service-to-service | `INTERNAL_SECRET` env on every container | none (intra-host) |

`TOKEN_SECRET` itself lives in `.env` (mode 600). If it leaks, ALL of the
above need rotation.

## Gmail OAuth permanence

Stable refresh tokens need the OAuth app published in Google Cloud
Console (mode = Production). In "Testing" mode, Google invalidates
refresh tokens after 7 days of idle.

If revoked:
1. `scripts/gmail_oauth_helper.py` running in a container exposing
   `dima.veranda.my/start` route.
2. Owner opens that URL in their Chrome (already logged into all three Gmail).
3. Three clicks, three accounts, done.
4. Ingestor picks up the new refresh tokens next poll cycle (~5 min).

## Telegram userbot session

Don't share an `auth_key` between two clients (e.g. MCP client + Vera
userbot). Telegram silently routes updates to one of them, the other goes
deaf. Each client should be its own device — separate auth via SMS.

## Instagram session

instagrapi sessions are ban-prone (datacenter IP = suspicious). Mitigation:
keep request rate gentle (90s poll), reuse device fingerprint across
restarts (saved in session_json_enc), don't burst.

## Audit

`events` is append-only. No `UPDATE` or `DELETE` from outside
`triage_metadata`-related code. We don't have a dedicated audit_log table
yet — every important state change is observable via `events` of
source=`monitor` or `vera_memory`.

## Encryption key rotation

Never rotate `TOKEN_SECRET` without re-encrypting every encrypted column.
Procedure:

1. Stop all ingestors.
2. Decrypt every encrypted column with the OLD secret.
3. Re-encrypt with the NEW secret.
4. Update `.env`, restart.

There's no automated migration for this. If you ever need to do it, write
the script and commit it.

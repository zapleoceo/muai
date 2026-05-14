# Security rules — myAI project

## Secrets
- NEVER commit `.env`, `*.session`, or any file containing API keys / tokens.
- If a secret is accidentally committed — rotate it immediately, then remove from history.
- All secrets come from environment variables; never hardcode them.

## API tokens
- Tokens stored in `api_tokens` DB table, never in source code.
- Use `TokenManager` for all API key access — no direct DB reads in providers.
- Rotate tokens via the Settings UI or `TokenManager.add/remove`.

## Authentication
- Admin routes require `Depends(require_owner)` — never relax this.
- Deploy endpoint uses `DEPLOY_SECRET` Bearer token — keep it long and random.
- Session cookies are HMAC-signed via `SESSION_SECRET`.

## Telegram
- Verify webhook secret (`WEBHOOK_SECRET`) on every incoming update.
- Never log full message content at INFO level — use DEBUG.
- `OWNER_TELEGRAM_ID` gates admin dashboard access; never accept user-supplied IDs.

## Docker / SSH
- `docker.sock` is mounted only for the deploy endpoint — don't expose it further.
- SSH alias `hetzner-root` is for trusted operations only; no automated user-triggered SSH.
- Never run `docker compose down -v` in production — it destroys the DB volume.

## Database
- Migrations applied via `psql` directly — no Alembic in production container.
- Always confirm `DROP` / `TRUNCATE` operations with the user before executing.
- Never run `DELETE FROM` without a `WHERE` clause on production tables.

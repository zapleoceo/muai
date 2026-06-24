# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml`:

1. **rsync** vera3/ to `hetzner-root:/var/www/vera3/` (excludes `.env`, `.git`, `__pycache__`, `*.session`)
2. **docker compose build && up -d --remove-orphans**
3. **smoke**: probes `vera3-gateway`, `vera3-brain-search`, `vera3-dashboard` /healthz + HTTPS `dima.veranda.my/login`
4. **prune** dangling images on success

## Tests gate

`.github/workflows/vera3-tests.yml` runs on every push touching `vera3/**`:

- Unit tests in `vera3/tests/unit/`
- Coverage gate: **40%** on `vera_shared` + `gateway` (raise as we cover more)
- Ruff + mypy are `continue-on-error` (non-blocking but visible)

Tests must pass before merging. The deploy workflow does NOT depend on the
tests workflow today — they run in parallel. The deploy can ship even with
red tests. **TODO**: gate deploy on tests when the suite is comprehensive
enough.

## Docs gate

`.github/workflows/docs-check.yml` blocks pushes that change Python under
`vera3/services/` or `vera3/shared/` without touching `vera3/docs/`.
Opt-out: `docs-not-needed` literal in any commit in the range.

## Monitor

`/usr/local/bin/vera3-monitor` — Bash script run by cron `*/5 * * * *`.
Checks 11 dimensions:

1. All key vera3-* containers up
2. `brain-triage` has ≥1 replica
3. `/healthz` on gateway, brain-search, dashboard
4. HTTPS dashboard reachable through Cloudflare
5. Disk usage <85% (warn) / <92% (critical)
6. Postgres `pg_isready`
7. Gmail accounts polled in last 30 min
8. Telegram events flowing in last 1h (userbot disconnected detection)
9. Triage backlog <5k (warn) / <10k (critical)
10. ≥1 LLM token available (not all in cooldown)
11. SSL cert expiry on `aib.zapleo.com` Origin cert <14 days

Alerts to `@Dimondra_Ai_Bot` DM to `OWNER_TELEGRAM_ID`. State-file
throttle 30 min. Recovery messages on flip back to healthy.

## Secrets

Server `.env` at `/var/www/vera3/infra/.env` (mode 600):

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | postgres root |
| `TOKEN_SECRET` | Fernet for `tokens.token_encrypted` (legacy fallback) |
| `INTERNAL_SECRET` | gateway X-Internal-Secret |
| `OWNER_TELEGRAM_ID` | `169510539` |
| `TELEGRAM_BOT_TOKEN` / `_USERNAME` | `@Dimondra_Ai_Bot` |
| `TELEGRAM_API_ID` / `_HASH` / `_PHONE` | Telethon MTProto |
| `GMAIL_CLIENT_ID` / `_SECRET` | OAuth app |
| `BROKER_URL` | `https://aib.zapleo.com` |
| `BROKER_PROJECT_KEY` | one-shot from broker `/admin/projects` |
| `VERA_DAILY_GLOBAL_CAP_USD` | hard global LLM spend cap |

## Backup

Postgres volume is the only persistent state. Manual snapshot:

```
ssh hetzner-root "docker exec vera3-postgres pg_dump -U vera vera | gzip > /var/backups/vera3-$(date +%F).sql.gz"
```

## Disaster recovery — Gmail token revoked

Most common incident. See `security.md` for full re-auth runbook.

Short version:
1. Run `scripts/gmail_oauth_helper.py` (Docker exec)
2. Open `https://dima.veranda.my/start` in Chrome
3. Click through TG-widget-style OAuth flow
4. Helper writes new refresh tokens, ingestor picks them up next poll

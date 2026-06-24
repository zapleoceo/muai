# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml` runs **three jobs**:

1. **`docs` job** — fails if any change under `vera3/services/` or
   `vera3/shared/` lands without a matching change under `vera3/docs/`.
   Opt-out per commit: literal `docs-not-needed`.
2. **`test` job** — pytest must pass; coverage gate currently **60%** on
   `vera_shared` + `gateway`.
3. **`deploy` job** — `needs: [docs, test]`. SSH to the server with a
   restricted key. The key is wired in `/root/.ssh/authorized_keys` to
   `command="/usr/local/bin/vera3-deploy"` — connecting at all runs the
   wrapper, anything the client sends is ignored.

The wrapper does:

1. `git clone` (or `git fetch + reset --hard origin/master`) the muai repo
   into `/var/www/muai-checkout/`.
2. `rsync vera3/ → /var/www/vera3/` preserving `.env`, sessions, pycache.
3. `docker compose build && up -d --remove-orphans` in `/var/www/vera3/infra`.
4. Polls `vera3-gateway /healthz` for up to 60 seconds, exits 11 if dead.

## Tests gate (separate workflow)

`.github/workflows/vera3-tests.yml` also runs on every push (independent
of deploy) and is the same pytest invocation. The duplication is
intentional: tests workflow shows up as a clean check on every PR, deploy
workflow re-runs them as a guard before shipping.

## Docs gate

`.github/workflows/docs-check.yml` blocks pushes that change Python under
`vera3/services/` or `vera3/shared/` without touching `vera3/docs/`.
Opt-out: `docs-not-needed` literal in any commit in the range.

## Restricted SSH key

Generated once on a dev box:
```
ssh-keygen -t ed25519 -f vera3_gh_deploy -N "" -C "github-actions-vera3-deploy"
```

Public part appended to `/root/.ssh/authorized_keys`:
```
command="/usr/local/bin/vera3-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA…
```

If this key leaks, the worst an attacker can do is re-run our wrapper.
No shell, no scp, no port-forward, no agent-forward.

Stored in GH Secrets as `HETZNER_SSH_KEY_VERA3`. The old (full-root)
`HETZNER_SSH_KEY` is no longer used by Vera's deploy and can be removed.

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

# /logs

Live logs from a Vera 3 production container.

## Usage

- `/logs` — last 100 lines of `gateway`
- `/logs brain-triage 300` — N lines of a named service
- `/logs error` — errors only, across all services

## Commands

```bash
# One service
ssh hetzner-root "cd /var/www/vera3/infra && docker compose logs --tail=100 gateway"

# Everything, errors only
ssh hetzner-root "cd /var/www/vera3/infra && docker compose logs --tail=500 2>&1 | grep -E 'ERROR|CRITICAL|Traceback|Exception'"

# Follow (bound it — never leave an open stream)
ssh hetzner-root "cd /var/www/vera3/infra && timeout 30 docker compose logs -f --tail=20 gateway"

# A single scaled triage replica
ssh hetzner-root "docker logs vera3-brain-triage-1 --tail=100"
```

Services: `gateway`, `dashboard`, `brain-search`, `brain-triage`,
`bot-telegram`, `ingestor-gmail`, `ingestor-telegram`,
`ingestor-instagram`, `media-worker`, `postgres`, `prune`.

Containers are `vera3-<service>`, except `brain-triage` which is scaled to
5: `vera3-brain-triage-1` … `-5`.

## Reading common failures

| In the log | Meaning | Where to look |
|---|---|---|
| `Permission denied (publickey)` on the deploy step | restricted deploy key rotated | `vera3/docs/deploy-ops.md` |
| Telethon `AuthKeyUnregisteredError`, ingestor crash-looping | userbot session revoked | re-auth in the dashboard: `/api/telegram/start` |
| Gmail `invalid_grant` | refresh token revoked | re-auth per account via `/api/gmail/oauth/start` |
| `429` / broker `cooldown` | LLM token rate-limited | AIbroker token pool, `vera3/docs/llm-broker.md` |
| Triage stuck in `processing` | worker died mid-claim | the watchdog in `background_loops.py` recovers it; if not, check `events.triage_status` |

## Notes

- The host also runs `aibroker-*` and `stepan2-*`. `docker compose` scoped
  to `/var/www/vera3/infra` will not touch them — bare `docker logs` can.
- Docker logs are size-capped and logrotated (`vera3/infra/logrotate`), so
  deep history is not in the container — query Postgres instead.

# /deploy

Trigger a production deploy of Vera on Hetzner.

## Steps

1. Show last 3 commits that will be deployed: `git log --oneline -3`
2. Push to master if not already (deploy script pulls from master).
3. Run: `ssh hetzner-root "/usr/local/bin/vera-deploy [service]"`
   - Default service: `vera-core`
   - Other valid services: `vera-gmail`, `vera-telegram`, `vera-coder`
4. The script does: `git pull → docker compose build --no-cache <svc> → up -d → image prune → builder prune --keep-storage=1gb` and posts a deploy event to vera-core.
5. Tail last 30 log lines: `ssh hetzner-root "docker compose -f /var/www/vera/docker-compose.yml logs --tail=30 <service>"`
6. Report success or first error from the log tail.

## Status check (a.k.a. `/status`)

`ssh hetzner-root "docker compose -f /var/www/vera/docker-compose.yml ps"` and `ssh hetzner-root "cd /var/www/vera && git log --oneline -1"` — compare server HEAD to local HEAD.

## Notes
- Never use `--force` flags with git on the server.
- If the build fails, show the full error and stop — do not attempt a rollback automatically.
- Project dir: `/var/www/vera`; containers: `vera-vera-core-1`, `vera-vera-gmail-1`, `vera-vera-telegram-1`, `vera-vera-coder-1`.
- SSH alias: `hetzner-root` (port 9617). Live URL: https://dima.veranda.my.
- Auto-monitor runs every 5 min via cron (`/usr/local/bin/vera-monitor`) and posts alerts to Vera.

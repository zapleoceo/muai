# /deploy

Trigger a production deploy to the Hetzner server.

## Steps

1. Show last 3 commits that will be deployed (`git log --oneline -3`)
2. Run: `ssh hetzner-root "cd /var/www/tgbot && git pull && docker compose build bot && docker compose up -d bot"`
3. Tail the last 30 log lines: `ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml logs --tail=30 bot"`
4. Report success or any errors found in the log tail.

## Notes
- Never use `--force` flags with git on the server.
- If the build fails, show the full error and stop — do not attempt a rollback automatically.
- Container is `tgbot-bot-1`, project dir is `/var/www/tgbot`.

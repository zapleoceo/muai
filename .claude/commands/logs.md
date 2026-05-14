# /logs

Fetch live logs from the production bot container.

## Usage
- `/logs` — last 100 lines
- `/logs 200` — last N lines (pass as argument)
- `/logs error` — grep for ERROR/WARNING lines only

## Command
```bash
ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml logs --tail={N} bot"
```

For errors only:
```bash
ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml logs --tail=500 bot 2>&1 | grep -E 'ERROR|WARNING|CRITICAL|Traceback|Exception'"
```

## Notes
- Container: `tgbot-bot-1`
- If you see `TelegramClient disconnected`, check userbot session file.
- `429` in logs means token rate-limit — check token cooldowns in Settings UI.

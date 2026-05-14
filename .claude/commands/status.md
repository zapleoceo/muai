# /status

Check the health of the production deployment.

## Steps
Run all checks in parallel:

1. **Container status**: `ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml ps"`
2. **Recent errors**: `ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml logs --tail=200 bot 2>&1 | grep -c 'ERROR\|CRITICAL'"`
3. **Last deploy commit on server**: `ssh hetzner-root "cd /var/www/tgbot && git log --oneline -1"`
4. **Local HEAD**: `git log --oneline -1`

## Report format
```
Server: <commit hash> <message>
Local:  <commit hash> <message>
Containers: <running / stopped>
Errors in last 200 log lines: <N>
```

If server commit != local HEAD, note that a deploy may be needed.

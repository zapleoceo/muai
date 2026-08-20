# /status

Health of the Vera 3 production deployment.

## Checks

```bash
# 1. Containers
ssh hetzner-root "cd /var/www/vera3/infra && docker compose ps"

# 2. Service health (gateway :8001, brain-search :8002, dashboard :8003)
ssh hetzner-root 'for p in 8001 8002 8003; do printf "%s " $p; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:$p/healthz; done'

# 3. Errors in the last 200 lines, per service
ssh hetzner-root "cd /var/www/vera3/infra && docker compose logs --tail=200 2>&1 | grep -cE 'ERROR|CRITICAL|Traceback'"

# 4. Deployed commit
ssh hetzner-root "cd /var/www/muai-checkout && git log --oneline -1"

# 5. Triage backlog
ssh hetzner-root "docker exec vera3-postgres psql -U vera -d vera -tAc \"SELECT triage_status, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC\""

# 6. Host resources (disk warn >85%, mem is tight: 3.7Gi total)
ssh hetzner-root "df -h / | tail -1; free -h | head -2"

# 7. Last monitor run
ssh hetzner-root "tail -20 /var/log/vera3-monitor.log"
```

Local HEAD: `git log --oneline -1`.

## Report format

```
Server:     <sha> <message>
Local:      <sha> <message>
Containers: <N up / N expected>
Health:     gateway <code>  brain-search <code>  dashboard <code>
Triage:     pending <N>  error <N>  dead <N>
Host:       disk <N%>  mem <used/total>
Errors:     <N> in last 200 log lines
```

If server sha != local HEAD, say a deploy is pending — do not deploy
without being asked.

## Notes

- `brain-triage` runs 5 replicas: `vera3-brain-triage-1` … `-5`.
- The host also runs unrelated stacks (`aibroker-*`, `stepan2-*`) — do not
  touch them when acting on Vera.
- `/usr/local/bin/vera3-monitor` runs every 5 min via root cron and DMs the
  owner via `@Dimondra_Ai_Bot`. It checks 11 dimensions, so an absent alert
  is itself signal.

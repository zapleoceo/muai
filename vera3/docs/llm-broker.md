# LLM via AIbroker

Vera no longer holds API keys for routing. All `chat()` and `embed()` calls
in `vera_shared/llm/client.py` go to **AIbroker** at `https://aib.zapleo.com`.

## Why

- Single source of truth for keys, cost tracking, cooldowns.
- One project (`vera`) in broker with `daily_cost_cap_usd=5.0`.
- New projects (Stepan, future) share the same pool — better utilization.
- Health monitor in broker pings every key every 10 min — Vera never has
  to manage that itself.

## How it works

Source: `shared/vera_shared/llm/broker_client.py`.

`client.py:chat()` checks `BROKER_URL + BROKER_PROJECT_KEY` env vars:

```python
if broker_enabled():
    try:
        return await chat_via_broker(...)
    except BrokerCallFailed:
        log.warning("broker down, falling back to local")
        # falls through to legacy path
```

So if the broker is unreachable, Vera transparently uses its local
`tokens` table as a cold fallback. We don't lose triage during an outage.

## Env vars

| Var | Value (server `.env`) |
|---|---|
| `BROKER_URL` | `https://aib.zapleo.com` |
| `BROKER_PROJECT_KEY` | `aib_prj_…` (one-shot from broker `/admin/projects`) |
| `BROKER_TIMEOUT_S` | default `120` |

Set in `docker-compose.yml` for `brain-triage`, `brain-search`, `dashboard`.

## What gets logged where

When Vera calls broker:
- Broker writes `aibroker.usage_log` with `project='vera'`, `workflow=...`.
- Vera also writes a row to its own `usage_log` (mirror, for dashboard
  stats and per-workflow analytics).

Both should agree on `tokens_in/out` and `cost_usd`. If they don't, look at
network errors between broker timeout and Vera's `_log_usage`.

## Verifying it's working

```bash
# Broker-side: see Vera's calls
ssh hetzner-root "docker exec aibroker-postgres psql -U aibroker -d aibroker -c \"
  SELECT u.workflow, u.provider, COUNT(*) AS calls
  FROM usage_log u JOIN projects p ON p.id=u.project_id
  WHERE p.name='vera' AND u.created_at > now() - interval '1 hour'
  GROUP BY 1,2 ORDER BY 3 DESC\""

# Vera-side: same period, should match approximately
ssh hetzner-root "docker exec vera3-postgres psql -U vera -d vera -c \"
  SELECT provider, workflow, COUNT(*) AS calls
  FROM usage_log WHERE created_at > now() - interval '1 hour'
  GROUP BY 1,2 ORDER BY 3 DESC\""
```

## Local pool deprecation plan

After 7 days of broker-only operation without fallback events, delete
`tokens` table and remove the legacy code in `client.py`. Until then,
`/tokens` page on dashboard shows the local pool as "fallback inventory".

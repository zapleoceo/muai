# LLM via AIbroker (broker-only mode)

Since 2026-06-26 Vera is **broker-only**. There is no local-token fallback
in `client.py` anymore. `chat()` and `embed()` either succeed via
[AIbroker](https://aib.zapleo.com) or raise `LLMCallFailed`. The local
`tokens` table is dormant — kept in the DB as an emergency reserve, not
read at runtime.

## Why fully on broker

- Single source of truth for keys, cost tracking, cooldowns.
- One project (`vera`) in broker with `daily_cost_cap_usd=5.0`.
- New projects (Stepan, …) share the same pool — better utilization.
- Health monitor in broker pings every key every 10 min.
- Vera-side code stays tiny: just `broker_client.py` + a 70-line
  `client.py` facade. No routing chains, no cost guards, no provider
  registry to maintain.

## How it works

```
   Vera                         AIbroker
   ─────────                    ──────────
   chat()                       /v1/chat?capability=chat:fast
     │                            │
     ├── _require_broker()        ├── pick_and_reserve() — chain free-first
     ├── chat_via_broker() ──────►├── check_caps()
     │                            ├── call_llm(provider, key, …)
     │                            └── record_usage()
     │  ◄── 200 {text,meta} ─────┘
     ├── _log_usage()  (mirror row to vera.usage_log)
     └── return (text, meta)
```

If broker returns non-2xx or network error → `BrokerCallFailed` →
re-raised as `LLMCallFailed`. Caller decides:
- **brain-triage** worker: returns event to `pending` status; next tick
  retries (see `worker.py:255`).
- **bot-telegram**: sends user a soft "временно недоступно".
- **brain-search**: returns 502 to the dashboard call.

## What got deleted

- `vera_shared/llm/cost_guard.py` — broker now decides caps
- `vera_shared/llm/registry.py` — broker knows providers
- `vera_shared/llm/routing.py` reduced to a `Capability` Literal alias
- `vera_shared/tokens/repository.py` + `tokens/model.py` — local pool ops
- `client.py` 470 → 70 lines (broker facade only)
- Dashboard `/tokens` is now a stub redirect to broker dashboard

## What survives

- `tokens` table in Postgres (kept as cold reserve at user request)
- `vera_shared/tokens/crypto.py` — Fernet helpers, used by ingestors to
  encrypt Gmail OAuth refresh tokens, IG sessionid, TG userbot sessions
  (these are NOT LLM tokens — different domain)
- `usage_log` table — broker_client mirrors every call into it so
  dashboard charts keep working without hitting broker

## Env vars

| Var | Value (server `.env`) |
|---|---|
| `BROKER_URL` | `https://aib.zapleo.com` |
| `BROKER_PROJECT_KEY` | `aib_prj_…` (one-shot from broker `/admin/projects`) |
| `BROKER_TIMEOUT_S` | default `120` |

Set in `docker-compose.yml` for `brain-triage`, `brain-search`,
`bot-telegram`, `dashboard`. If either `BROKER_URL` or
`BROKER_PROJECT_KEY` is missing at runtime, `chat()`/`embed()` raise
immediately at first call — fail-fast.

## Monitoring broker availability

`vera3-monitor.sh` (cron `*/5 * * * *`) probes `${BROKER_URL}/healthz`.
Logic:
- 1 failed probe → silent (transient — maybe deploy in progress).
- 2 consecutive failures (≥10 min down) → Telegram alert
  `broker_offline` with throttle 60 min.
- First successful probe after a streak → `recover` Telegram message.

State counter: `/var/lib/vera3-monitor/broker_fail_streak`.

## Resuming after an outage

The triage worker is self-healing. Events stay in `triage_status='pending'`
while broker is down (ingestors keep writing them in). When broker comes
back, the next `_claim_batch` tick grabs the oldest pendings in batches
of `BATCH_SIZE=50` per worker (3 replicas, configurable). A 10-min
outage at typical Vera traffic (~1 msg/min) yields ~10 pending events,
cleared in one tick.

## Emergency fallback (if broker is down for hours)

Only the LLM path is broken; ingest keeps writing. If you must restore
service before broker is fixed:

```bash
ssh hetzner-root
# 1. Re-add the legacy local code (revert this commit's client.py)
# 2. Or — manual triage from a local Python:
docker exec -it vera3-bot-telegram python -c "..."
```

The `tokens` table still has 24 active keys, so manual fallback is possible.
But normal recovery path is "wait for broker, queue drains itself".

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

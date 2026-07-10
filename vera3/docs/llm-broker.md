# LLM via AIbroker (broker-only mode)

Since 2026-06-26 Vera is **broker-only**, and since 2026-06-29 it holds
**no LLM keys at all**. `chat()`, `embed()`, vision and transcribe either
succeed via [AIbroker](https://aib.zapleo.com) or raise `LLMCallFailed`.
The `tokens` table was **dropped** (migration 008) — there is no local
pool, dormant or otherwise. Every provider key lives in the broker.

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

## Async jobs (`/v1/jobs`) — the default path since 2026-07-10

`chat()`/`chat_via_broker()` hold a connection open for the whole call —
a slow provider can 504 the caller. The broker also exposes a submit+poll
shape for any chat capability (`chat:fast/smart/code/edit/deep`,
`structured`, `prefilter`, `translate`, `vision` — NOT
`embedding`/`transcription`, those stay sync, they're fast):
`POST /v1/jobs?capability=X` → `202 {job_id, poll_url, poll_after_s}`,
then `GET /v1/jobs/{id}` until `status` is `done`/`error` (broker times
out a stuck job to `error` after ~20 min server-side).

`broker_client.chat_async_via_broker()` / `client.chat_async()` mirror
`chat_via_broker()`/`chat()`'s exact signature and error contract
(`BrokerCallFailed`/`LLMCallFailed`) — same `usage_log` mirroring via
`_log_usage()`, same `request_id`/`key_label` capture.
`JOB_POLL_DEADLINE_S` (env `BROKER_JOB_DEADLINE_S`, default **120s** —
matches the old sync `BROKER_TIMEOUT_S` so callers get the same latency
ceiling they had before) bounds the poll loop client-side, independent
of the broker's own ~20-min stale-job timeout.

**Every chat-capability call site in Vera is on `chat_async()`** (migrated
2026-07-10, ahead of the broker disabling sync `/v1/chat`): brain-triage's
`worker.py` (`triage_one` + `triage_group_batch`, 16k+/day — the main
reason this exists), `vera_shared/graph/rel_extract.py`, brain-search's
`agent.py` (ReAct loop step) + `app.py` (direct-answer synthesis), and
media-worker's vision recognition (previously raw httpx bypassing
`broker_client` entirely — now also gets `usage_log` mirroring for free).
`chat()`/`chat_via_broker()` themselves are unchanged and still exist —
nothing in Vera calls them anymore, but removing them is a separate,
deliberate cleanup once the broker's sync endpoint is actually retired.

**Load-tested** (10 concurrent `chat_async()` calls against the live
broker, matching brain-triage's `CONCURRENCY=10`): completed in ~4s
wall-clock, well inside the 120s deadline. Found and fixed one real bug
in the process — `_log_usage` used `meta.get("provider", "broker")`-style
defaults, but `dict.get(key, default)` only falls back when the key is
*absent*; a broker response with `provider`/`model` present-but-`null`
(seen once under the 10-way burst — likely a broker-side race, not a
Vera bug) sailed straight through as `None` into `usage_log`'s `NOT NULL`
columns and crashed the insert. Fixed via `meta.get(...) or fallback`,
which catches both "missing" and "present but null/empty".

## What got deleted

- `vera_shared/llm/cost_guard.py` — broker now decides caps
- `vera_shared/llm/registry.py` — broker knows providers
- `vera_shared/llm/routing.py` reduced to a `Capability` Literal alias
- `vera_shared/tokens/` package — entire local pool (repository, model,
  crypto moved out). Removed 2026-06-29.
- the token ORM model + the `tokens` Postgres table (migration 008)
- `usage_log.token_id` FK column (migration 008)
- `client.py` 470 → 86 lines (broker facade only)
- bot `/stats` no longer counts local keys; dashboard `/tokens` is an
  info page pointing at the broker

## What survives

- `vera_shared/crypto.py` — Fernet helpers (moved here from
  `tokens/crypto.py`), used by ingestors to encrypt Gmail OAuth refresh
  tokens, IG sessionid, TG userbot sessions. These are session secrets,
  NOT LLM keys — different domain.
- `usage_log` table — broker_client mirrors every call into it so
  dashboard charts keep working without hitting broker. Also captures
  `request_id`/`key_label` from the broker's response (see
  `domain-model.md`).
- The shared `httpx.AsyncClient` in `broker_client._client()` is built
  under an `asyncio.Lock` with a double-checked `if _http is None` — two
  concurrent first-callers used to be able to race and each construct
  their own client, leaking one.

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

## If the broker is down for hours

Only the LLM path is affected; ingest keeps writing, so nothing is lost —
events pile up in `triage_status='pending'` and drain once the broker is
back. There is no local fallback by design (Vera has no keys). The fix is
always "restore the broker", not "fail over inside Vera". The broker
itself has the redundancy: many keys across many providers, free-first
chains, health monitoring. `vera3-monitor.sh` alerts on Telegram if the
broker's `/healthz` is unreachable for ~10 min.

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

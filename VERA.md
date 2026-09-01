# Vera — single source of truth (top level)

> **Detailed docs live in [`vera3/docs/`](vera3/docs/).** This file is the
> orientation layer: what is live, what is dead, and where to look next.
> The v2-era spec that used to live here is archived at
> [`docs/VERA-v2-historical.md`](docs/VERA-v2-historical.md) — it describes
> SQLite and Neo4j Aura, **neither of which exists in production**. Do not
> act on it.

---

## 1. Vision

Vera is a **second self** — not a chatbot, not a triage queue. She holds
Dima's knowable context (events, people, projects, goals, values, voice)
and decides against the whole picture rather than the latest event.

Hard rules that still bind:

1. If something influences a decision, it is data in the graph — not a
   config file of preferences.
2. Source-agnostic: every input implements the same contract, no
   per-source branching in the core.
3. Owner-only authority. `OWNER_TELEGRAM_ID` is the single privileged
   identity.
4. One trigger → one decision. Re-processing the same event is a bug.

---

## 2. What is actually live

| Item | Value |
|---|---|
| Live URL | https://dima.veranda.my |
| Server | Hetzner VPS `ubuntu-2gb-nbg1-1`, SSH alias `hetzner-root` (port 9617) |
| Project dir | `/var/www/vera3` (compose at `/var/www/vera3/infra`) |
| Deploy checkout | `/var/www/muai-checkout` |
| State | **Postgres + pgvector** — container `vera3-postgres`, user `vera`, db `vera`, host `127.0.0.1:5433` |
| Graph | Materialized **inside Postgres** (`entities`, `entity_aliases`, `memberships`, `relationships`, `identity_nodes`, `patterns`) behind a `graph_repo` API |
| LLM | External **AIbroker** (`chat:fast` via `/v1/jobs`) — separate stack on the same host |
| Bot | `@Dimondra_Ai_Bot`, owner-only DM |
| Owner Telegram ID | `169510539` |

There is **no Neo4j** and **no SQLite** in production. The Neo4j swap
described in the archived spec was never executed; the `graph_repo`
indirection is what keeps it a future one-file change.

### Containers

`vera3-gateway`, `vera3-dashboard`, `vera3-brain-search`,
`vera3-brain-triage-1..5`, `vera3-bot-telegram`, `vera3-ingestor-gmail`,
`vera3-ingestor-telegram`, `vera3-ingestor-instagram`,
`vera3-media-worker`, `vera3-postgres`, `vera3-prune`.

Host ports (loopback only): gateway `8001`, brain-search `8002`,
dashboard `8003`, postgres `5433`.

The same host also runs unrelated stacks — `aibroker-*` and `stepan2-*`.
Scope every compose command to `/var/www/vera3/infra` so they are never
touched.

---

## 3. Repo layout — live vs legacy

**Live. Everything deployed comes from here:**

```
vera3/
├── shared/vera_shared/   # common lib: db, models, graph_repo, tools, llm
├── services/             # one Docker container each
│   ├── gateway/          # POST /event/<source>, X-Internal-Secret
│   ├── brain-triage/     # LLM classify workers (FOR UPDATE SKIP LOCKED)
│   ├── brain-search/     # ReAct agent + embedding search
│   ├── ingestor-{gmail,telegram,instagram}/
│   ├── bot-telegram/     # aiogram, owner-only
│   ├── dashboard/        # HTMX UI
│   └── media-worker/     # vision / OCR on attachments
├── infra/                # docker-compose.yml, migrations/, sql/, logrotate/
├── docs/                 # ← the real detailed documentation
├── tests/                # unit / service / integration
└── scripts/              # ops + import utilities
```

**Дерево Vera 2 удалено (2026-08-20).**

`vera-core/`, `vera-gmail/`, `vera-telegram/`, `vera-coder/`, `dashboard/`,
корневые `shared/`, `nginx/`, `cloudflare/`, `scripts/`, `backups/`,
`docker-compose.yml`, `.env.example`, `.scratch/` — 258 файлов, больше
половины репозитория. Не собирались и не деплоились, но засоряли поиск по
коду и всплывали в security-аудитах (`shared/vera_shared/tokens/`,
`vera-gmail/app/credentials.py`) уже после того, как репозиторий стал
публичным. История цела: `git show 60646413^:vera-core/app/main.py`.

Из исторических документов остались `docs/VERA-v2-historical.md` и
`docs/vera3-tz.md` — оба с баннером «не руководство к действию».

Если ты меняешь поведение — ты меняешь что-то под `vera3/`.

---

## 4. Where the detail lives

| Question | Doc |
|---|---|
| Service topology, event lifecycle, module-by-module breakdown | [`vera3/docs/architecture.md`](vera3/docs/architecture.md) |
| Deploy pipeline, CI gates, monitor, backups | [`vera3/docs/deploy-ops.md`](vera3/docs/deploy-ops.md) |
| Triage, patterns, consolidation, tuning history | [`vera3/docs/brain.md`](vera3/docs/brain.md) |
| Tables, columns, invariants | [`vera3/docs/domain-model.md`](vera3/docs/domain-model.md) |
| Identity / values / goals / style layer | [`vera3/docs/identity.md`](vera3/docs/identity.md) |
| Source contract, adding a new source | [`vera3/docs/sources.md`](vera3/docs/sources.md) |
| Слушатель разговоров на ноутбуке (`vera-listener/`) | [`vera3/docs/listener.md`](vera3/docs/listener.md) |
| LLM routing, token tiers, cost caps | [`vera3/docs/llm-broker.md`](vera3/docs/llm-broker.md) |
| Vision/OCR pipeline | [`vera3/docs/media-worker.md`](vera3/docs/media-worker.md) |
| Entity dedup and merge | [`vera3/docs/graph-dedup.md`](vera3/docs/graph-dedup.md) |
| HTTP surface | [`vera3/docs/api.md`](vera3/docs/api.md) |
| Auth boundaries, secrets | [`vera3/docs/security.md`](vera3/docs/security.md) |
| Code style (binding) | [`vera3/docs/conventions.md`](vera3/docs/conventions.md) |

---

## 5. Deploy — the short version

```bash
git push origin master
```

Push to `master` runs `.github/workflows/deploy.yml`: **docs gate → tests
(coverage 70%) → quality (ruff `E,F,W,I,B,UP,SIM,C4,RET`, vulture,
diff-cover 75%, docs name-sync) → deploy**. Any failing gate blocks the
deploy.

Manual fallback — `ssh hetzner-root /usr/local/bin/vera3-deploy`. It takes
**no arguments** and always ships `origin/master`: fetch + `reset --hard`,
`rsync -az --delete vera3/ → /var/www/vera3/` (preserving `.env`,
`infra/.env`, `*.session`), `compose build && up -d --remove-orphans`,
then poll gateway `/healthz` for 60s.

> **Server edits are not durable.** The rsync is `--delete`. Anything
> patched live and not pushed to `master` is silently reverted by the next
> deploy — including deploys unrelated to your change. This cost ~20 files
> of hotfixes in 2026-07, one of which was masking a production crash that
> promptly returned. Commit the same fix before calling an incident closed.

Migrations are **never** run by the deploy. See
[`.claude/commands/migrate.md`](.claude/commands/migrate.md) — and note
there is no migration tracking table, so migrations must be written
idempotently.

Monitor: `/usr/local/bin/vera3-monitor`, root cron `*/5`, 11 dimensions,
alerts by DM to the owner. Backup: `/usr/local/bin/vera-backup.sh`, cron
03:30.

---

## 6. Access model

| What | How |
|---|---|
| Server | `ssh hetzner-root` — full root. Key `D:\Projects\hetzner\hetzner_195.201.31.49_ed25519`, alias in `~/.ssh/config`. |
| CI deploy | Restricted key pinned to `command="/usr/local/bin/vera3-deploy"` in `/root/.ssh/authorized_keys`. GH secret `HETZNER_SSH_KEY_VERA3`. If it leaks, the worst it can do is re-run the wrapper. |
| Git | Remote is SSH `git@github.com:zapleoceo/muai.git`. Write access via a repo deploy key (`~/.ssh/muai_github_ed25519`). The Git Credential Manager entry belongs to account `dimondra`, which has **no write access** — HTTPS pushes return 403. |

Secrets never enter the repo: `.env`, `infra/.env`, and `*.session` are
gitignored and excluded from the deploy rsync. `detect-private-key` runs
as a pre-commit hook.

---

## 7. Known gaps

- **No migration tracking table.** Nothing records which of
  `vera3/infra/migrations/*.sql` has been applied. Numbering also skips
  `018` and `019`.
- ~~Coverage gate is 40% in `vera3-tests.yml` but 70% in `deploy.yml`~~ —
  **исправлено 2026-09-01.** Оба воркфлоу гоняют один и тот же гейт
  (`vera3/scripts/check_coverage.py`) с порогом НА КАЖДЫЙ пакет, а не одним
  процентом на репозиторий. Прежние 70% считались по двум пакетам из
  двенадцати, то есть описывали 38.8% продового кода; настоящая цифра по
  всему коду — 70.8%.
- **Host memory is tight**: 3.7 GiB total, ~2 GiB in use with 5 triage
  replicas. Scaling `brain-triage` up needs a resize first.
- **Legacy tree is still in the repo** (§3). It pollutes greps and search
  tools, and nothing builds from it.

---

## 8. Migration log

- **2026-08-20**: This file rewritten to match production. The v2 spec it
  used to contain (SQLite at `/data/vera.db`, Neo4j Aura, `vera-core`
  containers, `vera-deploy`) was stale in every operational detail and is
  archived at `docs/VERA-v2-historical.md`. `.claude/commands/*` were
  pointing at `/var/www/tgbot` and `/var/www/vera` — rewritten against the
  real `vera3` infra.
- **2026-06-09**: v3 substrate consolidated. Graph materialized inside
  Postgres behind `graph_repo` (Neo4j deferred indefinitely). Phase 3
  (Voice / per-relationship Style) promoted ahead of Phase 2. Tool layer
  formalized under `shared/vera_shared/tools/`.
- **2026-06-01**: Brain auto-feedback loop killed — monitor ignores
  Graphiti ingest errors. `/tool/{name}` requires `X-Internal-Secret`.
- **2026-05-22**: v3 spec adopted — single graph, no per-event config.
  Triage/persona/Trigger/DecisionReplay deprecated.
- **2026-05-21**: v2 in production — topics, replay table, threshold-based
  auto, scattered prefs. Now legacy.

Older detail: [`docs/VERA-v2-historical.md`](docs/VERA-v2-historical.md).

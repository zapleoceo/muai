# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml` runs **four jobs**:

1. **`docs` job** — any file changed under `vera3/services/` or
   `vera3/shared/` must be matched by a change under `vera3/docs/`.
   Opt-out per commit: literal `docs-not-needed`.
2. **`test` job** — pytest must pass; total coverage gate **70%** on
   `vera_shared` + `gateway`.
3. **`quality` job** — strict static analysis on the diff:
   - **Ruff** with extended ruleset `E,F,W,I,B,UP,SIM,C4,RET` — no
     warnings tolerated (`SIM` = simplify, `C4` = comprehensions,
     `RET` = unreachable-after-return).
   - **Vulture** dead-code detector on the files this push touched
     (`--min-confidence 80`) — surfaces unused funcs, classes, vars
     that ruff's `F401`/`F841` miss.
   - **Diff-cover** — every new/changed line must be ≥75% covered by
     tests in this PR (separate from the repo-wide 70% gate). Caught:
     "added a function without a test".
   - **Docs name-sync** — extract every public symbol added/removed in
     the diff (lowercase `def foo`, PascalCase `class Bar`; skip
     `_private`, `test_*`, dunders). Each **added** name must appear
     somewhere in `vera3/docs/`; each **removed** name must NOT remain
     in `vera3/docs/` (orphaned reference = stale doc). Opt-out:
     `docs-not-needed`.
4. **`deploy` job** — `needs: [docs, test, quality]`. SSH to the server
   with a restricted key wired in `/root/.ssh/authorized_keys` to
   `command="/usr/local/bin/vera3-deploy"` — anything the client sends
   is ignored.

### ⚠️ Manual server edits are NOT durable

`vera3-deploy` does `git reset --hard origin/master` in the checkout,
then `rsync -az --delete` that checkout onto `/var/www/vera3/` (env files
and sessions excluded, everything else replaced). Any file edited or
`scp`'d directly onto the server **that isn't committed to `master`**
gets silently wiped back to whatever `master` last had, the next time
this pipeline runs (push to master, or a manual `workflow_dispatch`) —
even if that run isn't about your change at all. This already happened
once (2026-07): a long session's worth of uncommitted hotfixes across
~20 files got reverted in one shot, including a fix that was masking a
production crash, which promptly came back. Database migrations are safe
(they're not part of the file sync — Postgres state persists
independently), but application code is not. If you patch the server
directly for a live incident, commit-and-push the same fix before
considering it done — otherwise it's living on borrowed time.

### What this guarantees

Any commit that reaches production has: passing tests, ≥75% coverage on
the actual changes, no dead code in the touched files, no syntax/import
nits, every public name documented, no orphan references to removed
code. If any of those fails, deploy is **blocked** until fixed — you
don't have to remember to check anything yourself.

The wrapper does:

1. `git clone` (or `git fetch + reset --hard origin/master`) the muai repo
   into `/var/www/muai-checkout/`.
2. `rsync vera3/ → /var/www/vera3/` preserving `.env`, sessions, pycache.
3. `docker compose build && up -d --remove-orphans` in `/var/www/vera3/infra`.
4. Polls `vera3-gateway /healthz` for up to 60 seconds, exits 11 if dead.

## Tests gate (separate workflow)

`.github/workflows/vera3-tests.yml` also runs on every push (independent
of deploy) and is the same pytest invocation. The duplication is
intentional: tests workflow shows up as a clean check on every PR, deploy
workflow re-runs them as a guard before shipping.

## Docs gate

`.github/workflows/docs-check.yml` blocks pushes that change Python under
`vera3/services/` or `vera3/shared/` without touching `vera3/docs/`.
Opt-out: `docs-not-needed` literal in any commit in the range.

## Restricted SSH key

Generated once on a dev box:
```
ssh-keygen -t ed25519 -f vera3_gh_deploy -N "" -C "github-actions-vera3-deploy"
```

Public part appended to `/root/.ssh/authorized_keys`:
```
command="/usr/local/bin/vera3-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA…
```

If this key leaks, the worst an attacker can do is re-run our wrapper.
No shell, no scp, no port-forward, no agent-forward.

Stored in GH Secrets as `HETZNER_SSH_KEY_VERA3`. The old (full-root)
`HETZNER_SSH_KEY` is no longer used by Vera's deploy and can be removed.

## Docs gate

`.github/workflows/docs-check.yml` blocks pushes that change Python under
`vera3/services/` or `vera3/shared/` without touching `vera3/docs/`.
Opt-out: `docs-not-needed` literal in any commit in the range.

## Monitor

`/usr/local/bin/vera3-monitor` — Bash script run by cron `*/5 * * * *`.
Checks 11 dimensions:

1. All key vera3-* containers up
2. `brain-triage` has ≥1 replica
3. `/healthz` on gateway, brain-search, dashboard
4. HTTPS dashboard reachable through Cloudflare
5. Disk usage <85% (warn) / <92% (critical)
6. Postgres `pg_isready`
7. Gmail accounts polled in last 30 min
8. Telegram events flowing in last 1h (userbot disconnected detection)
9. Triage backlog <5k (warn) / <10k (critical)
10. ≥1 LLM token available (not all in cooldown)
11. SSL cert expiry on `aib.zapleo.com` Origin cert <14 days

Alerts to `@Dimondra_Ai_Bot` DM to `OWNER_TELEGRAM_ID`. State-file
throttle 30 min (or `monitor_throttle_min` setting — see below).
Recovery messages on flip back to healthy.

## Runtime settings (`/settings` dashboard page)

Monitor thresholds and the backfill rate limit are editable at runtime
from `/settings` — no redeploy needed. Registry: `vera_shared.control.SETTINGS`.
Values live in `app_control` (same KV table as `backfill_paused`); the
Bash monitor script reads them directly via `psql` on each tick.

| Setting | Default | What it does |
|---|---|---|
| `monitor_throttle_min` | 30 min | Repeat-alert cooldown per alert key |
| `monitor_backlog_enabled` | on | Whether to alert on triage backlog size at all (turn off during a known-large backfill) |
| `triage_backlog_warn` / `_huge` | 5000 / 10000 | Pending-event thresholds for the two backlog alert levels |
| `backfill_max_per_hour` | 0 (unlimited) | Even-tempo cap on triage+media LLM requests/hour, shared globally across all replicas — see `brain.md` |
| `cluster_label_deadline_s` | 240 с | Сколько ждать free-пул на подпись кластера графа (фоновая задача может ждать дольше интерактивных 120с) |
| `cluster_label_retries` | 2 | Повторы запроса ярлыка при таймауте, потом фолбэк «кластер N» |
| `graph_hub_percentile` | 99 % | Узлы со степенью выше перцентиля исключаются из кластеризации как сверх-хабы (см. `identity.md`) |

Deploy-time parameters (replicas, concurrency, batch size) are shown
read-only on the same page for reference — they require a redeploy to
change (`docker-compose.yml` / server `.env`).

## Project membership sync

`ingestor-telegram/sync_projects.py` populates `project_membership`
(migration 010) from Telegram folders + chat-name rules + Gmail account
patterns — the deterministic source of truth `brain_triage/worker.py`
uses to override the LLM's `project` guess. See `domain-model.md` for
the table shape and matching rules.

Run manually (uses the ingestor's live Telethon session):
```bash
docker exec vera3-ingestor-telegram python -m ingestor_telegram.sync_projects
```

Not on a cron yet — folder/name-rule membership changes rarely (new
project chat added, folder reorganized). Re-run by hand after either.
Safe to re-run anytime: every write is idempotent (`ON CONFLICT ...
DO UPDATE`), and `derive_people()` does a clean delete+reinsert of
`kind='person'` rows each run.

**Deploy-order caution:** the very first run after migration 010 lands
should happen *before* any triage batch executes the membership-override
UPDATE in `worker.py` — otherwise that override's third query (reset
LLM-guessed itstep/veranda to `other` for chats not yet in
`project_membership`) will wipe existing classifications on an empty
table. Safe if triage is paused (`backfill_paused=1`) while you apply
the migration and run the sync once.

## Secrets

Server `.env` at `/var/www/vera3/infra/.env` (mode 600):

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | postgres root |
| `TOKEN_SECRET` | Fernet for Gmail refresh tokens & session cookies (no `tokens` table) |
| `INTERNAL_SECRET` | gateway X-Internal-Secret |
| `OWNER_TELEGRAM_ID` | `169510539` |
| `TELEGRAM_BOT_TOKEN` / `_USERNAME` | `@Dimondra_Ai_Bot` |
| `TELEGRAM_API_ID` / `_HASH` / `_PHONE` | Telethon MTProto |
| `GMAIL_CLIENT_ID` / `_SECRET` | OAuth app |
| `BROKER_URL` | `https://aib.zapleo.com` |
| `BROKER_PROJECT_KEY` | one-shot from broker `/admin/projects` |
| `VERA_DAILY_GLOBAL_CAP_USD` | hard global LLM spend cap |

## Backup

Ночной cron `30 3 * * * /usr/local/bin/vera-backup.sh` (исходник —
`vera3/scripts/vera-backup.sh`, ставится вручную). Схема:

- `/var/backups/vera/daily/YYYY-MM-DD/` — дампы всех БД проекта; **vera —
  без данных `event_embeddings`** (`VERA_DAILY_EXCLUDE`, дефолт): вектора —
  80% объёма и производные данные, ежедневно их таскать незачем. Плюс
  `secrets-env.tar.gz` (все .env — без TOKEN_SECRET дампы бесполезны)
  и SHA256SUMS. Ротация `KEEP_DAILY_DAYS` (5).
- `/var/backups/vera/weekly/YYYY-MM-DD/` — полный vera.dump с эмбеддингами
  раз в неделю (`FULL_DOW`, ISO-день, дефолт 7 = воскресенье). Ротация
  `KEEP_WEEKLY_DAYS` (14). Потеря недели эмбеддингов восстановима
  доэмбеддингом хвоста событий.

Все параметры — env-переменные скрипта, не правки кода.

### Backups → Synology NAS (за NAT)

NAS сам **забирает** дерево бэкапов с сервера (исходящее соединение — NAT
не мешает, проброс портов не нужен). На сервере — пользователь
`verabackup`: key-only, в `authorized_keys` зашита команда
`/usr/bin/rrsync -ro /var/backups/vera` — ключ физически не может ничего,
кроме read-only rsync этой папки. Группа `verabackup` имеет `g+rX` на
дерево (скрипт поддерживает это на каждом прогоне).

Приватный ключ: `/root/verabackup_nas_key` (отдать NAS, из чата не
светить). На Synology: Task Scheduler → ежедневный root-скрипт:

```
rsync -az --delete \
  -e "ssh -p 9617 -i /volume1/backup/vera_key -o StrictHostKeyChecking=accept-new" \
  verabackup@<server-ip>:/ /volume1/backup/vera/
```

(источник `:/` — rrsync уже прибил корень к /var/backups/vera).

Ручной снапшот по-прежнему:

```
ssh hetzner-root "docker exec vera3-postgres pg_dump -U vera vera | gzip > /tmp/vera3-$(date +%F).sql.gz"
```

## Disaster recovery — Gmail token revoked

Most common incident. See `security.md` for full re-auth runbook.

Short version:
1. Run `scripts/gmail_oauth_helper.py` (Docker exec)
2. Open `https://dima.veranda.my/start` in Chrome
3. Click through TG-widget-style OAuth flow
4. Helper writes new refresh tokens, ingestor picks them up next poll

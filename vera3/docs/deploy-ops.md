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
2. `exec` **`vera3/infra/deploy.sh` из свежего чекаута** — вся логика деплоя
   живёт в репозитории и правится пушем, а не ssh-ом на сервер. На сервере
   остаётся только обёртка, прибитая к ключу через `authorized_keys command=`.

`deploy.sh` делает:

1. `rsync vera3/ → /var/www/vera3/` preserving `.env`, sessions, pycache.
2. `docker compose build && up -d --remove-orphans` в `/var/www/vera3/infra`.
3. **Проверку каждого сервиса** (см. ниже), иначе выход 12.

Только проверка, без деплоя: `bash /var/www/vera3/infra/deploy.sh --verify-only`.

### Проверка вместо одной галочки шлюза

До 2026-08-27 проверка была одна: отвечает ли `/healthz` у шлюза. Остальные
шестнадцать контейнеров — media-worker, пять реплик триажа, пять ингесторов,
дашборд, поиск, бот, прунер — могли лежать или крутиться на ПРОШЛОМ образе, а
деплой всё равно возвращал ноль. Молчаливый деплой хуже упавшего: код на диске
новый, в контейнере старый, и увидеть это можно только залезнув внутрь
контейнера руками.

Теперь после `up -d` проверяется:

1. каждый сервис из `docker compose config --services` имеет контейнер
   (сервис без контейнера вовсе `ps -a` не показывает — ловится отдельно);
2. все контейнеры, включая реплики, в состоянии `running`;
3. образ контейнера совпадает с текущим id своей ссылки — то есть сборка не
   осталась «в столе», пока контейнер доживает на старом слое;
4. объявленный healthcheck не в состоянии `unhealthy`;
5. шлюз отвечает на `/healthz` (как и раньше).

Ссылка на образ берётся из самого контейнера (`.Config.Image`), а НЕ из вывода
`docker compose ps`. Это не придирка: `ps` печатает ссылку, только пока она
разрешается в этот же образ, а как только тег уехал на новую сборку — печатает
sha контейнера, и сравнение sha с самим собой всегда совпадает. Первая версия
проверки на этом и молчала — ровно в том случае, ради которого написана.

Проверка проверена поломкой: остановленный контейнер и уехавший тег дают выход
12 и строку `ПЛОХО` с именем виновника; здоровое состояние — «все сервисы живы
и на свежих образах».

### Замок: два деплоя одновременно ломают стек

Поймано вживую 2026-08-27: ручной прогон совпал с прогоном CI, `docker compose
up` упал на конфликте имени контейнера (`vera3-brain-triage-2`), и стек
остался с двадцатью контейнерами в `Created` и неотвечающим шлюзом. У CI есть
своя `concurrency`-группа, но она не знает ни про ручные запуски, ни про
второй раннер. Замок на файле (`flock`, `/var/lock/vera3-deploy.lock`)
знает про всех.

- **полный деплой** при занятом замке выходит сразу, код **15**: ждать нечего,
  следующий push задеплоит свежее;
- **`--verify-only`** ждёт до `LOCK_WAIT_S` (180с): посреди чужого деплоя
  проверка увидела бы полустакан и закричала ложной тревогой. Не дождалась —
  код **16** с явным сообщением, а не выдуманный вердикт;
- **замок не открывается** (нет `/var/lock`, права) — код **17**, отдельно от
  15. Смешивать нельзя: «занято» безопасно пропустить, а «замок сломан» — это
  поломка инфраструктуры, и если научить CI глотать 15 вместе с 17, деплои
  однажды прекратятся вообще без единого алерта.

**Замок берёт ОБЁРТКА, а не `deploy.sh`** — и это принципиально, нашло ревью.
Обёртка мутирует ОБЩИЙ чекаут (`git fetch` + `reset --hard`), из которого
`deploy.sh` потом делает `rsync`. Замок внутри скрипта защищал бы только
сборку и подъём, а порча источника успевала бы случиться раньше: второй деплой
переписывал бы чекаут под первым во время его rsync. Проверено вживую — при
занятом замке обёртка выходит с кодом 15 и в её логе **ноль** строк
`git fetch`, то есть чекаут не тронут.

Дескриптор наследуется через `exec`, поэтому `deploy.sh` замок не
перебирает: флаг `VERA3_DEPLOY_LOCK_HELD=1` говорит ему, что замок уже наш.
Без флага он взял бы НОВОЕ описание открытого файла и заблокировался на замке
собственного родителя.

Содержимое обёртки (она на сервере, в репозитории её нет — иначе некому было бы
обновить чекаут, чтобы её достать):

```sh
exec 9>/var/lock/vera3-deploy.lock || exit 17
flock -n 9 || { echo "другой деплой уже идёт"; exit 15; }
cd /var/www/muai-checkout && git fetch --quiet origin master
git reset --hard --quiet origin/master
export VERA3_DEPLOY_LOCK_HELD=1
exec bash vera3/infra/deploy.sh
```

### Уборка мусора: убирает тот, кто насорил

Замер 2026-08-27: на диске 38 ГБ свободно 8.5, при этом **4.76 ГБ образов
Docker (82% всех) никем не востребованы** плюс **3.74 ГБ кэша сборки**. Причина
по построению: каждый деплой пересобирает 13 образов, прежние остаются
висячими, а дневной крон чистит с фильтром `until=72h` — наш же мусор моложе
фильтра, и к 72 часам его накапливается ещё столько же.

Поэтому `collect_garbage` работает в конце успешного деплоя:

- `docker image prune -f` **без `-a`** — только висячие образы; чужие
  проекты машины (aibroker, stepan2) держат свои под тегами и не задеваются;
- `docker builder prune -f --filter until=24h` — кэш BuildKit **хоста**,
  общий на все проекты; фильтр давности тут единственная защита.

**Контейнер `vera3-prune` теперь делает ровно то же самое, раз в сутки.**
Раньше он гонял `docker system prune -af --filter until=72h`, и это было
единственное место, где vera3 могла снести чужое: `-a` удаляет любой образ,
на который в этот момент не смотрит контейнер, включая **тегированные**
образы aibroker и stepan2. Рассуждение выше (`image prune` без `-a` чужого
не трогает) было записано здесь же — но применялось только к деплою, а
суточный крон продолжал ходить по всему демону. Теперь пара одна и та же в
обоих местах.

Уборка — best-effort (`|| true`): её сбой не имеет права переворачивать
вердикт успешного деплоя в FAILURE. В `--verify-only` не выполняется.

Разовая чистка вживую дала: `system prune` с окном 24ч — 1.4 ГБ, кэш сборки —
2.34 ГБ, полный prune невостребованных образов — ещё 1.76 ГБ. Свободно
**8.5 → 10.34 ГБ**, все 28 контейнеров живы.

Попутно снято ложное подозрение, которое стоит помнить: `du -sh
/var/lib/docker/overlay2` показывал 16 ГБ при учёте образов в 4 ГБ, и это
выглядело как 12 ГБ осиротевших слоёв. Реального содержимого 6.6 ГБ — `du`
обходил смонтированные `merged` двадцати восьми живых контейнеров и считал их
файловые системы повторно. Мерить надо `du --exclude=merged` либо суммой
`overlay2/*/diff`.

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

`vera3/scripts/vera3-monitor.sh` — Bash script run by cron
`*/5 * * * * bash /var/www/vera3/scripts/vera3-monitor.sh`, **straight from the
deployed tree**, so a repo edit reaches the live monitor with the next deploy.
Until 2026-08-28 cron ran a hand-installed copy at `/usr/local/bin/vera3-monitor`
that the deploy never touched — the versioned file was decorative and nothing
said so. Do not reintroduce that copy.

Checks 12 dimensions:

1. Every service in `docker compose config` runs its declared replica count —
   the list is derived from compose, never spelled out in the script (below)
2. `/healthz` on gateway, brain-search, dashboard
3. HTTPS dashboard reachable through Cloudflare
4. Disk usage <85% (warn) / <92% (critical)
4b. **Host RAM** <87% (warn) / <93% (critical), measured as
   `MemAvailable` — page cache is reclaimable and doesn't show in `free`
4c. **OOM-kills in the last hour** (`journalctl -k`), reported separately:
   by the time a percentage check runs the memory is free again, so the
   kill itself is invisible to dimension 4b
5. Postgres `pg_isready`
6. Gmail accounts polled in last 30 min
7. Telegram events flowing in last 1h (userbot disconnected detection)
8. Triage backlog <5k (warn) / <10k (critical)
9. ≥1 LLM token available (not all in cooldown)
10. SSL cert expiry on `aib.zapleo.com` Origin cert <14 days

Alerts to `@Dimondra_Ai_Bot` DM to `OWNER_TELEGRAM_ID`. State-file
throttle 30 min (or `monitor_throttle_min` setting — see below).

### Container user and healthchecks

All images ran as **root** until 2026-09-01. On a box whose daemon is shared
with two other projects and where one container mounts `docker.sock`, that is
an unnecessary rung on the escalation ladder. Ten of eleven now run as uid
10001 (`USER vera`, added after `pip install` — installation writes to the
system `site-packages`).

`ingestor-telegram` is deliberately **still root**, and this is the one thing
to finish by hand. It writes its StringSession into the `vera3_tg_sessions`
volume; Docker only transfers ownership from the image onto an *empty*
volume, and that volume already exists in production owned by root. Adding
`USER` without fixing it would mean `Permission denied` on the session write
— i.e. the main data source down immediately after a deploy that runs
automatically on push to master. One-time fix on the host:

```bash
docker compose stop ingestor-telegram
docker run --rm -v vera3_tg_sessions:/s alpine chown -R 10001 /s
```

then add the same two lines to its Dockerfile.

`brain-triage` also gained a `HEALTHCHECK`. It was the only replicated
service with a real "process alive, doing nothing" failure mode (pool
exhaustion by background tasks) and no way to observe it: Docker and the
monitor both count containers and restarts, not progress. Liveness is a
file the worker touches at the top of every loop iteration — deliberately
**not** a DB probe, since a brief Postgres outage would then take down all
five replicas at once, and they are not what needs fixing.

### Memory ceilings (`mem_limit`)

Until 2026-09-01 **no container had a memory limit at all**, and the monitor
had no memory dimension either. On a 3.7 GiB box shared with `aibroker-*`
and `stepan2-*`, that meant a leak or one heavy batch anywhere let the
OOM-killer pick the victim — possibly in another project — and the only
trace was dimension 1 (a container went missing) or the restart-loop check,
i.e. always after the fact.

Every service now carries `mem_limit`. Two things to keep straight:

- It is a **ceiling, not a reservation**. The sum (~4.4 GiB) deliberately
  exceeds physical RAM. The point is to kill a *runaway* container instead
  of a random neighbour; slow collective growth is still the host's problem,
  which is what dimensions 4b/4c are for.
- The numbers are **upper-bound estimates, not measurements** — they were
  written without access to the live box. Under-sizing is the dangerous
  direction: too tight a limit kills a healthy container, i.e. causes the
  outage it is meant to prevent. First quiet hour on prod, run
  `docker stats --no-stream` and tighten them down to reality.

**Состав стека берётся из compose (2026-08-28).** Раньше монитор сверялся с
прибитым списком из семи имён, а сервисов двенадцать: `media-worker`,
`ingestor-slack`, `ingestor-trello`, `prune` не охранялись вовсе.

27.08 деплой (столкновение ручного прогона с CI) оставил снесёнными
`media-worker`, `ingestor-trello` и `bot-telegram` — поднялись они лишь
28.08 в 04:26, через 15 часов. Прибитый список подвёл обоими концами:

* `media-worker` и `ingestor-trello` в нём не значились — ни одной тревоги;
  распознавание картинок и голосовых стояло всю ночь, а нашлось по логу
  крона доливки, который упирался в мёртвый контейнер.
* `bot-telegram` в нём был, и монитор прислал **6 тревог за 15 часов**
  (13:10 → 04:20). Но рядом в каждой стоял `vera3-ingestor-instagram` —
  сервис, снятый ранее и забытый в списке. Сообщение прочли как шум про
  instagram: instagram убрали из списка (коммит `eb87701b`), тревога
  позеленела, мёртвый бот остался мёртвым ещё на четыре часа.

Мораль не «читать тревоги внимательнее», а убрать источник шума: снятый
сервис обязан уходить из охраны сам. Теперь список сервисов и число реплик
читаются из `docker compose config --format json`, поэтому новый сервис
охраняется с момента появления в compose, а снятый — перестаёт немедленно.
Отдельная проверка «хотя бы одна реплика brain-triage» ушла туда же: три
живых из пяти она считала нормой. Пустой ответ (мёртвый демон, сломанный
compose, нет `jq`) — это тревога, а не тишина: промолчать в такой момент
значит снять охрану со всего стека.

Проверить логику без побочных эффектов: `vera3-monitor.sh --check-containers`
печатает по строке на проблему и не трогает ни env, ни postgres, ни telegram
(тест `tests/unit/test_monitor_containers.py` гоняет её с подставным docker).

Что этой проверкой ПОКА не покрыто — чтобы не выглядело закрытым:

* Сервисы за `profiles:` (сейчас только `ingestor-instagram`) в вывод
  `docker compose config` не попадают и потому не охраняются — это и нужно.
  Проверено на compose `2.40.3`; поведение профилей между версиями менялось,
  так что при апгрейде стоит перепроверить, что выключенный сервис не
  вернулся в список и не начал слать тревоги, которые нечем чинить.
* Проверка `/healthz` (пункт 2) всё ещё ходит по прибитому списку
  `gateway brain-search dashboard`. Живой контейнер с повисшим приложением
  внутри нового сервиса она не заметит — тот же класс, отдельная задача.

**Anti-flapping (2026-08-06).** An alert now needs `monitor_fail_streak`
(default 2) consecutive failed checks; `recover()` resets the streak.
Before that a single bad check alerted immediately and the recovery wiped
the state file, so the 30-min throttle never engaged across an
alert→recover→alert cycle — the owner got pairs of «⚠️ no telegram events»
/ «✅ recovered» all night. The telegram-silence window also went 1 h → 3 h
(`monitor_tg_silence_h`): overnight traffic drops to 1-6 events/hour and a
completely empty *hour* is normal (measured 2026-08-05: 22:00 UTC had 0,
neighbours 2-5), while an empty three-hour stretch never occurred.

### Disk hygiene

Бокс 38 ГБ делится с aibroker/stepan2, поэтому Vera держит свой след
ограниченным:

- **Docker-логи**: демон-дефолт (`/etc/docker/daemon.json`) — 50m×3 на
  контейнер, т.е. до ~2 ГБ на одну Vera. Compose переопределяет его
  явным якорем `x-logging` (10m×3) для всех сервисов vera3.
- **Логи cron-скриптов** (`vera-backup`, `vera-media-requeue`,
  `vera3-monitor`, `vera3-sync-projects`; сам скрипт доливки очереди с
  2026-08-27 — `scripts/media_requeue.py`, крон запускает его через
  `docker exec -i vera3-media-worker python -`, имя лога прежнее): `infra/logrotate/vera` →
  `/etc/logrotate.d/vera`, weekly ×4 + compress, `su root adm`
  (без него logrotate отказывается: `/var/log` принадлежит `root:syslog`).
- **`shm_size: 256mb`** у postgres — Docker даёт `/dev/shm` 64 МБ, из-за чего
  `VACUUM events` падал с «could not resize shared memory segment»
  (2026-07-24). Автовакуум этим НЕ затронут — он всегда однопоточный;
  страдают ручной `VACUUM` и параллельные seq-scan'ы brain-search. Обход без
  перезапуска контейнера: `VACUUM (PARALLEL 0, ANALYZE) events`.
- Мёртвые строки в `events` (63k на 2026-07-24 после массовых UPDATE) — это
  НЕ поломка: порог автовакуума `50 + 0.2×live` ≈ 80k ещё не был достигнут.
  После разовых массовых правок статуса имеет смысл прогнать `VACUUM ANALYZE`
  руками, не дожидаясь порога.
- Разовые выгрузки (`*.csv`, `kb_*.sql`) в `/var/backups/vera/` — не часть
  схемы ротации, удалять вручную; для бэкапов есть per-project дерево.
Recovery messages on flip back to healthy.

## Runtime settings (`/settings` dashboard page)

Monitor thresholds and the backfill rate limit are editable at runtime
from `/settings` — no redeploy needed. Registry: `vera_shared.control.SETTINGS`.
Values live in `app_control` (same KV table as `backfill_paused`); the
Bash monitor script reads them directly via `psql` on each tick.

| Setting | Default | What it does |
|---|---|---|
| `monitor_throttle_min` | 30 min | Repeat-alert cooldown per alert key |
| `monitor_fail_streak` | 2 проверки | Сколько провалов ПОДРЯД до алерта (монитор раз в 5 мин → авария видна через ~10 мин, моргнувшая проверка молчит) |
| `monitor_tg_silence_h` | 3 ч | Окно тишины telegram до алерта «userbot отвалился» |
| `monitor_backlog_enabled` | on | Whether to alert on triage backlog size at all (turn off during a known-large backfill) |
| `triage_backlog_warn` / `_huge` | 5000 / 10000 | Pending-event thresholds for the two backlog alert levels |
| `backfill_max_per_hour` | 0 (unlimited) | Even-tempo cap on triage+media LLM requests/hour, shared globally across all replicas — see `brain.md` |
| `cluster_label_deadline_s` | 240 с | Сколько ждать free-пул на подпись кластера графа (фоновая задача может ждать дольше интерактивных 120с) |
| `cluster_label_retries` | 2 | Повторы запроса ярлыка при таймауте, потом фолбэк «кластер N» |
| `no_provider_cooldown_min` | 30 мин | Кулдаун circuit breaker'а после «no provider available» от брокера (см. `llm-broker.md`) |
| `budget_cap_cooldown_min` | 30 мин | То же после «daily budget cap reached». Пауза-проба, не дальше ближайшей 00:00 UTC — блокировка «до полуночи» останавливала vision на 23ч при живом пуле (инцидент 31.07) |
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

## Ретенция usage_log

`usage_log` растёт на строку с **каждого** LLM-вызова
(`broker_client._log_usage`) и до 2026-09-01 не чистилась ничем — политики
хранения в репозитории не было вообще. При 10-14 тыс. триажей в час это
сотни тысяч строк в сутки.

Дашборд при этом считал по ней агрегат без `WHERE`, то есть полным сканом
всей накопленной истории, на каждое обновление кэша (TTL 60 c, плюс поллинг
`/_progress` раз в 30 c, пока страница открыта). Теперь запрос ограничен
окном в 30 дней — самый широкий `FILTER` там и был `:month`, поэтому цифры
не изменились — а под окно добавлен `ix_usage_created_at` (миграция 029).
Существующие индексы не годились: `ix_usage_provider_date` ведёт с
`provider`, `ix_usage_event` — с `event_id`.

Чистка — отдельным крон-скриптом, не миграцией (удаление по времени
неидемпотентно и держало бы блокировку):

```cron
15 4 * * * docker exec -i vera3-postgres psql -qU vera -d vera \
             < /var/www/vera3/scripts/prune_usage_log.sql
```

Срок — 90 дней, правится одним числом в самом скрипте. Удаляет порциями по
50 тыс. с `COMMIT` в цикле, поэтому не держит долгую блокировку и не раздувает
WAL; на пустом хвосте выходит с первой итерации.

## Backup

Ночной cron `30 3 * * * /usr/local/bin/vera-backup.sh` (исходник —
`vera3/scripts/vera-backup.sh`, ставится вручную). Раскладка —
**пер-проектная**, сервер держит только короткий буфер, длинная история
живёт на Synology NAS:

Бэкапим только НЕвоспроизводимое: код приходит из git, а `event_embeddings`
(3.6 ГБ, 66% БД) пересчитываются из `events` через брокер — их данные
исключаются из **всех** vera-дампов (`VERA_EXCLUDE`, дефолт
`event_embeddings`). Схема таблицы в дампе остаётся, на restore пустая
таблица дозаполняется реэмбеддингом. Дамп vera: **129 МБ** вместо 1.8 ГБ.
`events`, граф, `usage_log`, `.env` — воссоздать нельзя, бэкапим.

- `/var/backups/vera/<project>/daily/YYYY-MM-DD/` (project ∈ aibroker,
  stepan2, vera) — дамп БД + `secrets.tar.gz` (`.env` проекта — без
  TOKEN_SECRET дампы бесполезны) + SHA256SUMS. Ротация `KEEP_DAILY_DAYS`
  (**2** — буфер на случай пропуска NAS-пула, не хранилище).
- `/var/backups/vera/vera/weekly/YYYY-MM-DD/` — тот же лёгкий vera.dump
  (тоже без `event_embeddings`), но дольше живёт: чекпоинт с бóльшим
  окном восстановления (`FULL_DOW`, 7 = воскресенье; `KEEP_WEEKLY_DAYS` 7).

Все параметры — env-переменные скрипта, не правки кода.

### Backups → Synology NAS (за NAT) — настроено 2026-07-14

NAS сам **забирает** дерево бэкапов (исходящее соединение — NAT не
мешает; QuickConnect для rsync не годится). Ключевая пара сгенерирована
НА NAS (задача «Zapleo keygen» в Task Scheduler, ключ
`/root/.ssh/hetzner_backup`) — приватный ключ никогда не покидал NAS.
На сервере — пользователь `verabackup`: key-only, в `authorized_keys`
зашита команда `/usr/bin/rrsync -ro /var/backups/vera` — ключ физически
не может ничего, кроме read-only rsync этой папки. Группа `verabackup`
имеет `g+rX` на дерево (скрипт поддерживает это на каждом прогоне).

На Synology — задача Task Scheduler «Zapleo backup pull» (root,
ежедневно 05:00):

```
KEY=/root/.ssh/hetzner_backup
DEST=/volume1/Backup/Zapleo
mkdir -p "$DEST"
rsync -az -e "ssh -p 9617 -i $KEY -o StrictHostKeyChecking=accept-new" verabackup@195.201.31.49:/ "$DEST/"
find "$DEST"/*/daily -maxdepth 1 -type d -name "20*" -mtime +60 -exec rm -rf {} \; 2>/dev/null
find "$DEST"/*/weekly -maxdepth 1 -type d -name "20*" -mtime +180 -exec rm -rf {} \; 2>/dev/null
exit 0
```

Без `--delete` — история накапливается на NAS и чистится своим
retention (60 дней daily / 180 weekly). Источник `:/` — rrsync уже
прибил корень к /var/backups/vera, дерево на NAS повторяет пер-проектную
структуру (`Zapleo/vera/…`, `Zapleo/aibroker/…`).

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

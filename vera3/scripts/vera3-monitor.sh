#!/bin/bash
# vera3-monitor: каждые 5 минут проверяет состояние стека и шлёт алерт в
#                Telegram при поломке. Throttle: один alert per key per 30 min.
#
# Установка — крон зовёт ЭТОТ файл прямо из развёрнутого дерева:
#   crontab -e → */5 * * * * bash /var/www/vera3/scripts/vera3-monitor.sh >> /var/log/vera3-monitor.log 2>&1
#
# Через `bash <путь>`, а не напрямую: так запуск не зависит от бита +x,
# который легко потерять при переносе дерева, — а потеряв его, крон замолчал
# бы точно так же тихо, как молчал прибитый список контейнеров.
#
# Копии в /usr/local/bin быть не должно. До 28.08.2026 крон запускал именно её,
# а деплой обновлял только /var/www/vera3/scripts/ — то есть правка в
# репозитории на живой монитор не влияла вовсе, и заметить это было нечем.
# Сюда же относится порядок редиректов: `2>&1 >> file` уводит stderr в почту
# крона, а не в лог, поэтому сначала файл, потом `2>&1`.
#
# Конфиг — берётся из /var/www/vera3/infra/.env (TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID).
set -u

ENV_FILE="/var/www/vera3/infra/.env"
STATE_DIR="/var/lib/vera3-monitor"
LOG_TAG="vera3-monitor"
COMPOSE_DIR="${COMPOSE_DIR:-/var/www/vera3/infra}"

# ─── состав стека: что должно быть поднято ──────────────────────────────────
# Список сервисов берётся из compose, а НЕ из перечня имён в этом файле.
# Прибитый список бьёт дважды, и оба раза молча.
#
# 27.08.2026 деплой оставил снесёнными media-worker, ingestor-trello и
# bot-telegram; поднялись они только через 15 часов.
#   * media-worker и ingestor-trello в списке НЕ ЧИСЛИЛИСЬ — про них монитор
#     не сказал ни слова, распознавание стояло всю ночь, и нашлось это по
#     логу крона доливки, который упирался в мёртвый контейнер.
#   * bot-telegram в списке был, и монитор честно прислал 6 тревог за 15
#     часов. Но в тех же тревогах стоял vera3-ingestor-instagram — сервис,
#     давно снятый и забытый в списке. Сообщение выглядело шумом про
#     instagram, его так и прочли: instagram убрали из списка, тревога
#     позеленела, а мёртвый bot-telegram остался мёртвым.
#
# Отсюда правило: список берётся из compose. Тогда снятый сервис уходит из
# охраны сам, и в тревоге не может оказаться имени, которое нечего чинить.
#
# Функция ничего не решает про алерты — печатает по строке на проблему. Так её
# гоняет тест с подставным docker (`vera3-monitor.sh --check-containers`), не
# поднимая ни env-файла, ни postgres, ни telegram.
# Тело в скобках, а не в фигурных: это подоболочка, поэтому `cd` ниже не
# утекает в остальной скрипт. Заходим в каталог ОДИН раз и падаем громко, если
# не вышло. Прежний вариант делал `cd` в каждой итерации через `&&`: не сработай
# он там, `running` осталось бы ПУСТЫМ, `[ "" -lt 1 ]` ругнулось бы в stderr и
# вернуло ложь — и сервис молча не засчитался бы как проблемный. Ровно тот
# класс тихого «всё хорошо», ради которого вся эта функция и переписана.
# Нашло ревью.
check_containers() (
    local spec svc want running problems=0
    cd "$COMPOSE_DIR" 2>/dev/null || {
        echo "нет каталога $COMPOSE_DIR — проверять состав стека не по чему"
        exit 1
    }
    # Число реплик — из compose, а не из головы: 3 живых из 5 у brain-triage
    # это тихая потеря 40% пропускной способности, и её надо видеть.
    # `// 1` в jq не считает 0 ложью, поэтому осознанный `replicas: 0` не
    # превращается в 1 и не даёт вечную тревогу о выключенном сервисе.
    local filter='.services | to_entries[] | "\(.key) \(.value.deploy.replicas // 1)"'
    spec=$(docker compose config --format json 2>/dev/null | jq -r "$filter" 2>/dev/null)
    if [ -z "$spec" ]; then
        # Пустой ответ — это НЕ «всё хорошо». Так выглядит мёртвый демон docker,
        # сломанный compose-файл или отсутствующий jq. Промолчать здесь значит
        # снять охрану со всего стека ровно тогда, когда она нужнее всего.
        echo "состав стека не читается из $COMPOSE_DIR (docker, compose-файл или jq)"
        exit 1
    fi
    # Через here-string, не через конвейер: `while` за пайпом уходит в
    # подоболочку, и счётчик problems терялся бы вместе с ней.
    while read -r svc want; do
        [ -z "$svc" ] && continue
        running=$(docker compose ps --status running -q "$svc" 2>/dev/null | grep -c .)
        if [ "$running" -lt "$want" ]; then
            echo "$svc: живых контейнеров $running из $want"
            problems=$(( problems + 1 ))
        fi
    done <<< "$spec"
    # Цена всей проверки — 3.3с на 12 сервисов (замер на сервере, 28.08):
    # отдельный вызов `docker compose ps` на сервис по 0.23с. Раз в 5 минут это
    # около процента времени, и ради простоты оно того стоит.
    [ "$problems" -eq 0 ]
)

if [ "${1:-}" = "--check-containers" ]; then
    check_containers
    exit $?
fi

mkdir -p "$STATE_DIR"

if [ ! -f "$ENV_FILE" ]; then
    logger -t "$LOG_TAG" "env file $ENV_FILE missing — aborting"
    exit 1
fi

TELEGRAM_BOT_TOKEN=$(grep ^TELEGRAM_BOT_TOKEN "$ENV_FILE" | cut -d= -f2-)
OWNER_TELEGRAM_ID=$(grep ^OWNER_TELEGRAM_ID "$ENV_FILE" | cut -d= -f2-)

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$OWNER_TELEGRAM_ID" ]; then
    logger -t "$LOG_TAG" "TELEGRAM_BOT_TOKEN or OWNER_TELEGRAM_ID empty"
    exit 1
fi

THROTTLE_MIN=30  # дефолт; переопределяется настройкой monitor_throttle_min

# ─── setting(key, default) ──────────────────────────────────────────────────
# Читает значение из app_control (редактируется в дашборде /settings).
# Пусто/ошибка → default. Так пороги и частота алертов меняются без передеплоя.
setting() {
    local key="$1"; local def="$2"; local v
    v=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
        "SELECT value FROM app_control WHERE key='${key}'" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$v" ]; then echo "$def"; else echo "$v"; fi
}

# Глобальная частота повтора алертов — из настройки (дефолт 30 мин).
THROTTLE_MIN=$(setting monitor_throttle_min 30)

# Сколько ПОДРЯД идущих неудачных проверок нужно, чтобы поднять алерт.
# Монитор крутится раз в 5 мин, поэтому 2 = реальная авария заметна через
# ~10 мин, а разовая моргнувшая проверка молчит. Без этого ночью шло по
# паре сообщений в час: пустой час → alert, следующее сообщение → recover,
# и так по кругу (recover стирает state-файл, поэтому throttle не спасал).
FAIL_STREAK=$(setting monitor_fail_streak 2)

# ─── streak-счётчики подряд идущих провалов ─────────────────────────────────
bump_streak() {   # key → печатает новое значение
    local f="$STATE_DIR/${1}.streak" n=0
    [ -f "$f" ] && n=$(cat "$f" 2>/dev/null || echo 0)
    n=$(( n + 1 ))
    echo "$n" > "$f"
    echo "$n"
}
clear_streak() { rm -f "$STATE_DIR/${1}.streak"; }

# ─── alert(key, message, [throttle_min], [min_streak]) ──────────────────────
# Молчит пока провалов подряд меньше min_streak, и пока с прошлого алерта по
# этому key не прошло throttle минут.
alert() {
    local key="$1"
    local msg="$2"
    local throttle="${3:-$THROTTLE_MIN}"   # опц. кастомный throttle в минутах
    local min_streak="${4:-$FAIL_STREAK}"
    local state_file="$STATE_DIR/$key"
    local now streak
    streak=$(bump_streak "$key")
    if [ "$streak" -lt "$min_streak" ]; then
        logger -t "$LOG_TAG" "ALERT held ($key, streak $streak/$min_streak): $msg"
        return
    fi
    now=$(date +%s)
    if [ -f "$state_file" ]; then
        local last
        last=$(cat "$state_file")
        local diff=$(( (now - last) / 60 ))
        if [ "$diff" -lt "$throttle" ]; then
            logger -t "$LOG_TAG" "ALERT throttled ($key, $diff min ago): $msg"
            return
        fi
    fi
    echo "$now" > "$state_file"
    logger -t "$LOG_TAG" "ALERT $key: $msg"
    curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
         -d "chat_id=${OWNER_TELEGRAM_ID}" \
         -d "parse_mode=HTML" \
         --data-urlencode "text=⚠️ <b>Vera 3 monitor</b>%0A${msg}" \
         -o /dev/null || true
}

# ─── recover(key) ────────────────────────────────────────────────────────────
# Если когда-то был алерт по key, а сейчас всё OK — шлём recovery и чистим.
recover() {
    local key="$1"
    local msg="$2"
    local state_file="$STATE_DIR/$key"
    clear_streak "$key"       # проверка прошла — серия провалов прервана
    if [ -f "$state_file" ]; then
        rm -f "$state_file"
        logger -t "$LOG_TAG" "RECOVER $key: $msg"
        curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
             -d "chat_id=${OWNER_TELEGRAM_ID}" \
             -d "parse_mode=HTML" \
             --data-urlencode "text=✅ <b>Vera 3 recovered</b>%0A${msg}" \
             -o /dev/null || true
    fi
}

# ─── 1. Весь состав стека поднят ────────────────────────────────────────────
# Сравнение идёт с объявленным числом реплик, поэтому отдельная проверка
# «хотя бы одна реплика brain-triage» больше не нужна — она входит сюда.
containers_bad=$(check_containers | paste -sd ';' - | sed 's/;/; /g')
if [ -n "$containers_bad" ]; then
    alert "containers_down" "Контейнеры: ${containers_bad}"
else
    recover "containers_down" "Весь состав стека поднят."
fi

# ─── 2. Health endpoints ─────────────────────────────────────────────────────
for svc in gateway brain-search dashboard; do
    if ! docker exec "vera3-$svc" python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz',timeout=5).status==200 else 1)" 2>/dev/null; then
        alert "healthz_$svc" "/healthz failed for vera3-$svc"
    else
        recover "healthz_$svc" "vera3-$svc /healthz OK."
    fi
done

# HTTPS dashboard через CloudFlare
http_code=$(curl -sf -o /dev/null -w "%{http_code}" -m 10 https://dima.veranda.my/login || echo "000")
if ! echo "$http_code" | grep -qE "^(200|303)$"; then
    alert "https_dashboard" "https://dima.veranda.my/login returned HTTP $http_code"
else
    recover "https_dashboard" "HTTPS dashboard reachable ($http_code)."
fi

# ─── 3. Диск ─────────────────────────────────────────────────────────────────
disk_pct=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
if [ "$disk_pct" -ge 92 ]; then
    alert "disk_critical" "Disk usage <b>${disk_pct}%</b> on /. Free space critical."
elif [ "$disk_pct" -ge 85 ]; then
    alert "disk_warn" "Disk usage ${disk_pct}% on /."
else
    recover "disk_critical" "Disk back to ${disk_pct}%."
    recover "disk_warn" "Disk back to ${disk_pct}%."
fi

# ─── 3b. Память хоста ────────────────────────────────────────────────────────
# Бокс — 3.7 ГиБ на ТРИ стека (vera3, aibroker, stepan2). До 2026-09-01 у
# контейнеров не было лимитов памяти вообще, и монитор память не смотрел ни в
# каком виде: авария всплывала задним числом, когда OOM-killer уже выбрал
# жертву и она попадала в проверку 1 (счёт контейнеров) или 10 (рестарт-петля).
# Считаем available, а не free: страничный кэш отдаётся под нагрузкой и в
# free не виден.
mem_total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
mem_avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
if [ -n "$mem_total" ] && [ "$mem_total" -gt 0 ]; then
    mem_used_pct=$(( (mem_total - mem_avail) * 100 / mem_total ))
    mem_avail_mb=$(( mem_avail / 1024 ))
    if [ "$mem_used_pct" -ge 93 ]; then
        alert "mem_critical" "RAM <b>${mem_used_pct}%</b> занято, свободно ${mem_avail_mb} МБ. OOM-killer близко."
    elif [ "$mem_used_pct" -ge 87 ]; then
        alert "mem_warn" "RAM ${mem_used_pct}% занято, свободно ${mem_avail_mb} МБ."
    else
        recover "mem_critical" "RAM back to ${mem_used_pct}%."
        recover "mem_warn" "RAM back to ${mem_used_pct}%."
    fi
fi

# ─── 3c. OOM-kill за последний час ───────────────────────────────────────────
# Отдельно от 3b: убийство уже случилось, память к моменту проверки свободна,
# и по одному лишь проценту это не видно никогда.
if command -v journalctl >/dev/null 2>&1; then
    oom_hits=$(journalctl -k --since '-1h' --no-pager 2>/dev/null \
                 | grep -ciE 'out of memory: killed process|oom-kill:' || true)
    if [ "${oom_hits:-0}" -gt 0 ]; then
        oom_who=$(journalctl -k --since '-1h' --no-pager 2>/dev/null \
                    | grep -iE 'out of memory: killed process' | tail -3 \
                    | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
        alert "oom_kill" "OOM-killer сработал ${oom_hits} раз(а) за час:
<pre>${oom_who}</pre>"
    else
        recover "oom_kill" "OOM-kill'ов за последний час нет."
    fi
fi

# ─── 4. Postgres reachable ───────────────────────────────────────────────────
if ! docker exec vera3-postgres pg_isready -U vera -d vera -q 2>/dev/null; then
    alert "postgres_down" "Postgres pg_isready failed."
else
    recover "postgres_down" "Postgres OK."
fi

# ─── 5a. Gmail polling freshness — только живые ящики ───────────────────────
# needs_reauth исключаем: это известное состояние (см. 5b), не «поллинг сломан».
stale_gmail=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT email FROM gmail_accounts WHERE is_active AND NOT needs_reauth AND (last_polled_at IS NULL OR last_polled_at < now() - interval '30 minutes')" \
    2>/dev/null | tr '\n' ',' | sed 's/,$//' )
if [ -n "$stale_gmail" ]; then
    alert "gmail_stale" "Gmail accounts not polled &gt;30 min: $stale_gmail"
else
    recover "gmail_stale" "Gmail polling fresh."
fi

# ─── 5b. Gmail re-auth needed — мягкое напоминание раз в 12ч ─────────────────
# Отдельный класс: токен отозван/без scope. Действие — кнопка в дашборде.
# Throttle 720 мин чтобы не спамить (Дима уже знает, чинит по кнопке).
reauth_gmail=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT email FROM gmail_accounts WHERE is_active AND needs_reauth" \
    2>/dev/null | tr '\n' ',' | sed 's/,$//' )
if [ -n "$reauth_gmail" ]; then
    alert "gmail_reauth" "Gmail ящики ждут переподключения: ${reauth_gmail}%0AОткрой https://dima.veranda.my/sources → «Переподключить Gmail» (оставь все галки)." 720
else
    recover "gmail_reauth" "Все Gmail ящики переподключены."
fi

# ─── 6. Telegram userbot — события льются ────────────────────────────────────
# Нет ни одного нового telegram-события за окно — userbot заглох.
# Окно 3ч, а не 1ч: ночью поток падает до 1-6 сообщений в час, и полностью
# пустой ЧАС — норма (замеряно 05.08: 22:00 UTC ровно 0 при 2-5 в соседних).
# Пустых трёх часов подряд в измерениях не было ни разу, так что сигнал
# остаётся честным, а ложные ночные срабатывания уходят.
TG_SILENCE_H=$(setting monitor_tg_silence_h 3)
tg_count=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COUNT(*) FROM events WHERE source='telegram' AND received_at > now() - interval '${TG_SILENCE_H} hours'" \
    2>/dev/null || echo "0")
if [ "${tg_count:-0}" -eq 0 ]; then
    alert "telegram_silent" "No new telegram events in last ${TG_SILENCE_H}h. Userbot possibly disconnected."
else
    recover "telegram_silent" "Telegram events flowing ($tg_count in last ${TG_SILENCE_H}h)."
fi

# ─── 7. Triage queue (пороги + частота настраиваются в дашборде) ─────────────
BACKLOG_ENABLED=$(setting monitor_backlog_enabled 1)
BACKLOG_WARN=$(setting triage_backlog_warn 5000)
BACKLOG_HUGE=$(setting triage_backlog_huge 10000)
pending=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COUNT(*) FROM events WHERE triage_status='pending'" 2>/dev/null || echo "0")
if [ "$BACKLOG_ENABLED" = "1" ]; then
    if [ "${pending:-0}" -gt "$BACKLOG_HUGE" ]; then
        alert "triage_backlog" "Triage backlog HUGE: ${pending} pending events."
    elif [ "${pending:-0}" -gt "$BACKLOG_WARN" ]; then
        alert "triage_warn" "Triage backlog ${pending} pending."
    else
        recover "triage_backlog" "Triage backlog OK (${pending})."
        recover "triage_warn" "Triage backlog OK (${pending})."
    fi
fi

# ─── 8. AIbroker reachable ──────────────────────────────────────────────────
# Vera работает только через брокер: если он лёг — встаёт триаж, бот и поиск.
# Алертим если ДВА тика подряд не получили /healthz (= ~10 мин при cron */5).
# State: $STATE_DIR/broker_fail_streak (счётчик consecutive 'down' тиков).
BROKER_URL_VAL=$(grep ^BROKER_URL "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r' | sed 's:/*$::')
if [ -z "$BROKER_URL_VAL" ]; then
    alert "broker_not_configured" "BROKER_URL не задан в .env — Vera не сможет звонить LLM."
else
    streak_file="$STATE_DIR/broker_fail_streak"
    if curl -sf -m 7 -o /dev/null "${BROKER_URL_VAL}/healthz"; then
        # success — сброс счётчика + recover-alert если был сбой
        if [ -f "$streak_file" ] && [ "$(cat "$streak_file")" -gt 0 ]; then
            echo 0 > "$streak_file"
            recover "broker_offline" "AIbroker (${BROKER_URL_VAL}) снова отвечает."
        else
            echo 0 > "$streak_file"
        fi
    else
        prev=$(cat "$streak_file" 2>/dev/null || echo 0)
        streak=$(( prev + 1 ))
        echo "$streak" > "$streak_file"
        # Первый промах — молча; со второго подряд (=≥10 мин при cron */5) — алерт.
        if [ "$streak" -ge 2 ]; then
            mins=$(( streak * 5 ))
            # throttle 60 мин чтобы Telegram не звенел каждые 5 минут
            alert "broker_offline" "AIbroker (${BROKER_URL_VAL}) не отвечает ${mins} мин — triage/бот/поиск встали." 60
        fi
    fi
fi

# ─── 9. Container restart loop detection ────────────────────────────────────
restarting=$(docker ps --filter 'status=restarting' --filter 'name=vera3' --format '{{.Names}}')
if [ -n "$restarting" ]; then
    alert "containers_restarting" "Containers in restart loop: $restarting"
else
    recover "containers_restarting" "No restart loops."
fi

# ─── 10. SSL cert expiry на dima.veranda.my (origin cert) ────────────────────
if [ -f /etc/ssl/vera/cert.pem ]; then
    end_date=$(openssl x509 -in /etc/ssl/vera/cert.pem -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -n "$end_date" ]; then
        end_epoch=$(date -d "$end_date" +%s 2>/dev/null || echo "0")
        now_epoch=$(date +%s)
        days_left=$(( (end_epoch - now_epoch) / 86400 ))
        if [ "$days_left" -lt 14 ] && [ "$days_left" -gt 0 ]; then
            alert "cert_expiring" "Vera Origin cert expires in ${days_left} days."
        elif [ "$days_left" -lt 0 ]; then
            alert "cert_expired" "Vera Origin cert EXPIRED ${days_left} days ago."
        else
            recover "cert_expiring" "Cert OK (${days_left} days)."
            recover "cert_expired" "Cert OK."
        fi
    fi
fi

# ─── 11. Daily LLM spend cap warning ────────────────────────────────────────
spent_today=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COALESCE(ROUND(SUM(cost_usd)::numeric, 2), 0) FROM usage_log WHERE created_at::date = current_date" \
    2>/dev/null || echo "0")
global_cap=$(grep ^VERA_DAILY_GLOBAL_CAP_USD "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "2.0")
# alert at 90% of cap
threshold=$(echo "$global_cap * 0.9" | bc 2>/dev/null || echo "1.8")
if awk "BEGIN { exit !($spent_today >= $threshold) }"; then
    alert "llm_cap_warn" "LLM spend today: \$${spent_today} (cap \$${global_cap})."
fi

exit 0

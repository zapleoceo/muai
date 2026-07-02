#!/bin/bash
# vera3-monitor: каждые 5 минут проверяет 11 dimensions и шлёт алерт в Telegram
#                при поломке. Throttle: один alert per key per 30 min.
#
# Установка:
#   sudo install -m 755 vera3-monitor.sh /usr/local/bin/vera3-monitor
#   crontab -e   →   */5 * * * * /usr/local/bin/vera3-monitor 2>&1 >> /var/log/vera3-monitor.log
#
# Конфиг — берётся из /var/www/vera3/infra/.env (TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID).
set -u

ENV_FILE="/var/www/vera3/infra/.env"
STATE_DIR="/var/lib/vera3-monitor"
LOG_TAG="vera3-monitor"

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

# ─── alert(key, message) ────────────────────────────────────────────────────
# Тишина если последний alert по этому key был меньше THROTTLE_MIN минут назад.
# Иначе — POST в Telegram + обновляет timestamp.
alert() {
    local key="$1"
    local msg="$2"
    local throttle="${3:-$THROTTLE_MIN}"   # опц. кастомный throttle в минутах
    local state_file="$STATE_DIR/$key"
    local now
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

# ─── 1. Все ключевые контейнеры подняты ─────────────────────────────────────
REQUIRED_CONTAINERS=(
    vera3-postgres
    vera3-gateway
    vera3-brain-search
    vera3-bot-telegram
    vera3-dashboard
    vera3-ingestor-gmail
    vera3-ingestor-telegram
    vera3-ingestor-instagram
)
down=()
for c in "${REQUIRED_CONTAINERS[@]}"; do
    if ! docker ps --format '{{.Names}}' | grep -q "^${c}$"; then
        down+=("$c")
    fi
done
if [ "${#down[@]}" -gt 0 ]; then
    alert "containers_down" "Containers down: $(IFS=,; echo "${down[*]}")"
else
    recover "containers_down" "All vera3 containers up."
fi

# Минимум 1 brain-triage реплика
triage_count=$(docker ps --filter 'name=brain-triage' --format '{{.Names}}' | wc -l)
if [ "$triage_count" -lt 1 ]; then
    alert "triage_replicas" "No brain-triage replicas running."
else
    recover "triage_replicas" "Triage replicas: $triage_count."
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
# Если за час нет ни одного нового telegram-события — userbot заглох
tg_count=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COUNT(*) FROM events WHERE source='telegram' AND received_at > now() - interval '1 hour'" \
    2>/dev/null || echo "0")
if [ "${tg_count:-0}" -eq 0 ]; then
    alert "telegram_silent" "No new telegram events in last 1h. Userbot possibly disconnected."
else
    recover "telegram_silent" "Telegram events flowing ($tg_count in last 1h)."
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

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

THROTTLE_MIN=30  # минут между повторными алертами одного и того же ключа

# ─── alert(key, message) ────────────────────────────────────────────────────
# Тишина если последний alert по этому key был меньше THROTTLE_MIN минут назад.
# Иначе — POST в Telegram + обновляет timestamp.
alert() {
    local key="$1"
    local msg="$2"
    local state_file="$STATE_DIR/$key"
    local now
    now=$(date +%s)
    if [ -f "$state_file" ]; then
        local last
        last=$(cat "$state_file")
        local diff=$(( (now - last) / 60 ))
        if [ "$diff" -lt "$THROTTLE_MIN" ]; then
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

# ─── 5. Gmail accounts — polling freshness ──────────────────────────────────
# Если last_polled_at старше 30 минут И аккаунт активен → проблема.
stale_gmail=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT email FROM gmail_accounts WHERE is_active AND (last_polled_at IS NULL OR last_polled_at < now() - interval '30 minutes')" \
    2>/dev/null | tr '\n' ',' | sed 's/,$//' )
if [ -n "$stale_gmail" ]; then
    alert "gmail_stale" "Gmail accounts not polled &gt;30 min: $stale_gmail"
else
    recover "gmail_stale" "Gmail polling fresh."
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

# ─── 7. Triage queue ─────────────────────────────────────────────────────────
pending=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COUNT(*) FROM events WHERE triage_status='pending'" 2>/dev/null || echo "0")
if [ "${pending:-0}" -gt 10000 ]; then
    alert "triage_backlog" "Triage backlog HUGE: ${pending} pending events."
elif [ "${pending:-0}" -gt 5000 ]; then
    alert "triage_warn" "Triage backlog ${pending} pending."
else
    recover "triage_backlog" "Triage backlog OK (${pending})."
    recover "triage_warn" "Triage backlog OK (${pending})."
fi

# ─── 8. LLM provider availability ────────────────────────────────────────────
# Хотя бы один токен не в cooldown — иначе всё стоит
alive_tokens=$(docker exec vera3-postgres psql -U vera -d vera -tAc \
    "SELECT COUNT(*) FROM tokens WHERE is_active AND (cooldown_until IS NULL OR cooldown_until < now())" \
    2>/dev/null || echo "0")
if [ "${alive_tokens:-0}" -lt 1 ]; then
    alert "llm_no_tokens" "ALL LLM tokens in cooldown/inactive. Brain is frozen."
else
    recover "llm_no_tokens" "LLM tokens alive: ${alive_tokens}."
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

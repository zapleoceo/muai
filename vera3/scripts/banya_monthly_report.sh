#!/usr/bin/env bash
# banya_monthly_report.sh — 1-го числа шлёт в Telegram-группу «Веранда и Баня»
# ОТ ИМЕНИ ДИМЫ отчёт бани за прошлый месяц:
#
#     Сумма без кальянов: 120 009 500
#     К выплате 24 001 900
#
# Раньше Дима писал это руками 1-го числа. Сумму считает veranda.my
# (GET /internal/banya-month-summary, заголовок X-Report-Secret), отправляет
# юзербот через POST /actions/send_message внутри контейнера
# vera3-ingestor-telegram (send_guard.py: свой секрет + allowlist чатов).
#
# Cron (root, сервер в UTC; 03:00 UTC = 10:00 Нячанг). Три запуска — страховка:
# если в 10:00 veranda.my недоступна, отчёт уйдёт в 12:00 или 15:00. Повторной
# отправки нет: после успеха ставится маркер sent-YYYY-MM.
#   0 3,5,8 1 * * bash /var/www/vera3/scripts/banya_monthly_report.sh >> /var/log/banya-monthly-report.log 2>&1
#
# Ручной прогон без отправки:   DRY_RUN=1 bash banya_monthly_report.sh
# Конкретный месяц:             YM=2026-09 DRY_RUN=1 bash banya_monthly_report.sh
#
# Конфиг из /var/www/vera3/infra/.env: BANYA_REPORT_SECRET, BANYA_REPORT_CHAT_ID,
# TELEGRAM_BOT_TOKEN + OWNER_TELEGRAM_ID (алерт владельцу при сбое).
set -u -o pipefail

ENV_FILE="${ENV_FILE:-/var/www/vera3/infra/.env}"
STATE_DIR="${STATE_DIR:-/var/lib/banya-monthly-report}"
REPORT_URL="${REPORT_URL:-https://veranda.my/internal/banya-month-summary}"
CONTAINER="${CONTAINER:-vera3-ingestor-telegram}"
DRY_RUN="${DRY_RUN:-0}"
YM="${YM:-}"

log() { echo "$(date -u '+%F %T')Z banya-report: $*"; }

env_get() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^"//' -e 's/"$//'; }

[ -f "$ENV_FILE" ] || { log "env file $ENV_FILE missing"; exit 1; }
REPORT_SECRET=$(env_get BANYA_REPORT_SECRET)
CHAT_ID=$(env_get BANYA_REPORT_CHAT_ID)
BOT_TOKEN=$(env_get TELEGRAM_BOT_TOKEN)
OWNER_ID=$(env_get OWNER_TELEGRAM_ID)

fail() {
    log "FAIL: $*"
    if [ "$DRY_RUN" != "1" ] && [ -n "$BOT_TOKEN" ] && [ -n "$OWNER_ID" ]; then
        curl -s -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
             -d "chat_id=${OWNER_ID}" \
             --data-urlencode "text=⚠️ Отчёт бани в «Веранда и Баня» НЕ отправлен: $*. Следующая попытка — по крону (10/12/15 по Нячангу), иначе отправь руками." \
             -o /dev/null || true
    fi
    exit 1
}

[ -n "$REPORT_SECRET" ] || fail "BANYA_REPORT_SECRET не задан в $ENV_FILE"
[ -n "$CHAT_ID" ]       || fail "BANYA_REPORT_CHAT_ID не задан в $ENV_FILE"

url="$REPORT_URL"
[ -n "$YM" ] && url="${url}?ym=${YM}"

json=$(curl -sS --fail-with-body -m 120 --retry 2 --retry-delay 20 \
            -H "X-Report-Secret: ${REPORT_SECRET}" "$url") \
    || fail "veranda.my не отдала сумму (${json:0:200})"

ym=$(printf '%s' "$json" | sed -n 's/.*"ym":"\([0-9]\{4\}-[0-9]\{2\}\)".*/\1/p')
[ -n "$ym" ] || fail "в ответе veranda нет ym: ${json:0:200}"

mkdir -p "$STATE_DIR"
marker="$STATE_DIR/sent-$ym"
if [ -f "$marker" ] && [ "$DRY_RUN" != "1" ]; then
    log "за $ym уже отправлено ($(cat "$marker")) — пропускаю"
    exit 0
fi

# Разбор JSON и отправка — питоном ВНУТРИ контейнера юзербота: на хосте не
# нужны ни jq, ни секрет отправки (TG_SEND_SECRET есть только в контейнере).
out=$(docker exec -e REPORT_JSON="$json" -e DRY_RUN="$DRY_RUN" "$CONTAINER" python -c '
import json, os, sys, urllib.request
d = json.loads(os.environ["REPORT_JSON"])
if not d.get("ok"):
    sys.exit("veranda: " + str(d.get("error")))
if int(d.get("without_vnd") or 0) <= 0:
    sys.exit("сумма без кальянов <= 0 — не отправляю")
text = d["text"]
if os.environ.get("DRY_RUN") == "1":
    print("DRY_RUN, would send:\n" + text)
    sys.exit(0)
req = urllib.request.Request(
    "http://127.0.0.1:8000/actions/send_message",
    data=json.dumps({"chat_id": sys.argv[1], "text": text}).encode(),
    headers={"Content-Type": "application/json",
             "X-Send-Secret": os.environ.get("TG_SEND_SECRET", "")},
)
try:
    r = json.load(urllib.request.urlopen(req, timeout=60))
except urllib.error.HTTPError as e:
    sys.exit("userbot HTTP %s: %s" % (e.code, e.read()[:200]))
print("sent message_id=%s" % r["message_id"])
' "$CHAT_ID" 2>&1) || fail "${out:0:300}"

log "$ym: $out"
if [ "$DRY_RUN" != "1" ]; then
    echo "$(date -u '+%F %T')Z ${out}" > "$marker"
fi

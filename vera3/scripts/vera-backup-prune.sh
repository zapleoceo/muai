#!/usr/bin/env bash
# Чистит локальные дампы в /var/backups/vera, но ТОЛЬКО те, что NAS явно подтвердил.
#
# Установка (не деплоится CI, копируется вручную; шаги — в vera3/docs/deploy-ops.md):
#   cron root: 20 * * * * /usr/local/bin/vera-backup-prune.sh >> /var/log/vera-backup-prune.log 2>&1
#
# Правило удаления файла F (*.dump, *.tar.gz):
#   1. в SHA256SUMS его каталога есть строка "<sha256>  ./F" (пишет vera-backup.sh);
#   2. рядом лежит F.nas-confirmed (обычный файл, не симлинк), первое слово которого
#      (hex, регистр и CR игнорируются) равно этому sha256;
#   3. mtime старше SETTLE_MIN минут и у файла нет открытых дескрипторов;
#   4. непосредственно перед rm sha256 самого файла пересчитан и равен записанному.
# Нет SHA256SUMS / нет строки / нет, пустое или чужое подтверждение — файл остаётся.
# atime НЕ учитывается: он доказывает лишь, что чтение началось, а не что NAS
# сохранил полную копию (13.10.2026 удалено 595 МБ после 70-секундной сессии NAS).
# Разовые дампы вне SHA256SUMS (pre-055-*.dump и т.п.) не удаляются никогда.
# SHA256SUMS и *.nas-confirmed не трогаются; каталог удаляется, только если пуст.
#
# ВАЖНО: подтверждающего пока НЕ существует — NAS ходит через `rrsync -ro` и писать
# обратно не может. Поэтому сейчас скрипт ничего не удаляет; место освобождает
# ротация vera-backup.sh (KEEP_DAILY_DAYS). Как рождается *.nas-confirmed — отдельное
# решение владельца, варианты в vera3/docs/deploy-ops.md (раздел Backup).
set -euo pipefail
ROOT=${ROOT:-/var/backups/vera}
SETTLE_MIN=${SETTLE_MIN:-30}
[ -d "$ROOT" ] || { echo "$(date +%F_%T) нет каталога $ROOT"; exit 0; }

freed=0
kept=0
unconf_bytes=0
now=$(date +%s)
declare -A reasons=()

keep() { # $1 причина, $2 размер
  kept=$((kept+1))
  reasons[$1]=$(( ${reasons[$1]:-0} + 1 ))
  case "$1" in no_sums|not_listed|no_confirm|empty_confirm|mismatch|changed) unconf_bytes=$((unconf_bytes+$2)) ;; esac
}

while IFS= read -r -d '' f; do
  sz=$(stat -c %s "$f" 2>/dev/null) || continue
  mt=$(stat -c %Y "$f" 2>/dev/null) || continue
  dir=$(dirname "$f"); name=$(basename "$f")
  sums="$dir/SHA256SUMS"
  [ -f "$sums" ] || { keep no_sums "$sz"; continue; }
  # имя — всё после разделителя "<sha>  " или "<sha> *", без "./"; пробелы в имени допустимы
  want=$(NAME="$name" awk '{sub(/\r$/,""); h=$1; r=substr($0,length(h)+2); sub(/^[ *]/,"",r); sub(/^\.\//,"",r); if (r==ENVIRON["NAME"]) {print tolower(h); exit}}' "$sums")
  [ -n "$want" ] || { keep not_listed "$sz"; continue; }
  conf="$f.nas-confirmed"
  { [ -f "$conf" ] && [ ! -L "$conf" ]; } || { keep no_confirm "$sz"; continue; }
  got=$(tr -d '\r' < "$conf" | awk 'NR==1 {print tolower($1); exit}')
  [ -n "$got" ] || { keep empty_confirm "$sz"; continue; }
  [ "$got" = "$want" ] || { keep mismatch "$sz"; continue; }
  [ $(( (now - mt) / 60 )) -ge "$SETTLE_MIN" ] || { keep unsettled "$sz"; continue; }
  if command -v fuser >/dev/null 2>&1 && fuser -s "$f" 2>/dev/null; then
    keep open_fd "$sz"; continue
  fi
  cur=$(sha256sum < "$f" 2>/dev/null | awk '{print tolower($1)}') || cur=""
  [ "$cur" = "$want" ] || { keep changed "$sz"; continue; }
  if rm -f "$f"; then freed=$((freed+sz)); else keep rm_failed "$sz"; fi
done < <(find "$ROOT" -type f \( -name '*.dump' -o -name '*.tar.gz' \) -print0)

find "$ROOT" -mindepth 1 -type d -empty -delete 2>/dev/null || true

summary=""
for r in "${!reasons[@]}"; do summary="$summary $r=${reasons[$r]}"; done
echo "$(date +%F_%T) удалено $((freed/1024/1024)) MB, оставлено файлов: $kept, неподтверждено: $((unconf_bytes/1024/1024)) MB, причины:${summary:- нет}"

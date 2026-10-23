#!/usr/bin/env bash
# Nightly backup of all project DBs + their .env secrets — per-project layout:
#
#   $DEST/<project>/daily/YYYY-MM-DD/<db>.dump + secrets.tar.gz + SHA256SUMS
#   $DEST/vera/weekly/YYYY-MM-DD/vera.dump   (longer-retention checkpoint, FULL_DOW)
#
# Сервер — только КОРОТКИЙ буфер (KEEP_DAILY_DAYS=2): длинную историю хранит
# Synology NAS, который каждую ночь забирает всё дерево read-only rrsync-юзером
# `verabackup` (см. docs/deploy-ops.md «Backups → Synology NAS»).
#
# Бэкапим только НЕвоспроизводимое: код приходит из git, а event_embeddings
# (3.6 ГБ, 66% БД) пересчитываются из events через брокер — их данные
# исключаем ИЗ ОБОИХ дампов (VERA_EXCLUDE), схема таблицы остаётся, на
# restore она дозаполняется реэмбеддингом. Всё прочее (events, граф, .env,
# usage_log) воссоздать нельзя — бэкапим.
#
# С 2026-10-23 дампы БД Vera (daily и weekly) делает прямой NAS-поток
# (forced-command verastream → vera-stream → pg_dump, 02:00 VN), а не этот скрипт:
# SKIP_DB_PROJECTS=vera (дефолт). Секреты vera (secrets.tar.gz) по-прежнему
# пишутся здесь — поток их не несёт. Вернуть дамп: SKIP_DB_PROJECTS= (пусто).
#
# Restore DB : docker exec -i <container> pg_restore -c -U <user> -d <db> < file.dump
# Restore env: tar xzf secrets.tar.gz -C /
set -euo pipefail

DEST=${DEST:-/var/backups/vera}            # rrsync-root NAS-юзера — не менять
KEEP_DAILY_DAYS=${KEEP_DAILY_DAYS:-2}
KEEP_WEEKLY_DAYS=${KEEP_WEEKLY_DAYS:-7}
FULL_DOW=${FULL_DOW:-7}                    # ISO day-of-week: 7 = воскресенье
BACKUP_GROUP=${BACKUP_GROUP:-verabackup}   # NAS-пользователь читает через группу
# Таблицы, ДАННЫЕ которых не бэкапим (воспроизводимы) — схема остаётся.
VERA_EXCLUDE=${VERA_EXCLUDE:-${VERA_DAILY_EXCLUDE:-event_embeddings}}
# Проекты (csv), чьи дампы БД здесь НЕ делаются (секреты и SHA256SUMS — делаются).
SKIP_DB_PROJECTS=${SKIP_DB_PROJECTS-vera}
WWW_ROOT=${WWW_ROOT:-/var/www}              # корень .env проектов (тесты подменяют)

TODAY=$(date +%F)

dump() {  # container db user outdir [pg_dump extra args...]
  local container=$1 db=$2 user=$3 outdir=$4; shift 4
  if docker ps --format '{{.Names}}' | grep -qx "$container"; then
    docker exec "$container" pg_dump -U "$user" -d "$db" -Fc "$@" \
      > "$outdir/$db.dump"
    echo "  dumped $db → $outdir ($(du -h "$outdir/$db.dump" | cut -f1))"
  else
    echo "  SKIP $container (not running)"
  fi
}

skip_db() {  # project → 0, если дамп БД проекта перенесён в NAS-поток
  local IFS=, p
  for p in $SKIP_DB_PROJECTS; do [ "$p" = "$1" ] && return 0; done
  return 1
}

exclude_args() {  # csv table list → --exclude-table-data flags
  local IFS=,
  for t in $1; do printf -- '--exclude-table-data=%s ' "$t"; done
}

backup_project() {  # project container db user env_path [pg_dump extra...]
  local project=$1 container=$2 db=$3 user=$4 env_path=$5; shift 5
  local out="$DEST/$project/daily/$TODAY"
  mkdir -p "$out"
  if skip_db "$project"; then
    echo "  db dump of $project moved to NAS stream (vera-stream), skipped"
  else
    dump "$container" "$db" "$user" "$out" "$@"
  fi
  # Секреты проекта: без TOKEN_SECRET зашифрованные токены в дампе — мусор.
  if [ -f "$env_path" ]; then
    tar -czf "$out/secrets.tar.gz" -C / "${env_path#/}" \
      && echo "  secrets tarred ($project)"
  fi
  ( cd "$out" && sha256sum ./* > SHA256SUMS ) 2>/dev/null || true
}

echo "[$(date)] backup -> $DEST (per-project)"
backup_project aibroker aibroker-postgres aibroker aibroker "$WWW_ROOT/aibroker/.env"
backup_project stepan2  stepan2-postgres  stepan2  stepan2  "$WWW_ROOT/stepan2/infra/.env"
# shellcheck disable=SC2046
backup_project vera     vera3-postgres    vera     vera     "$WWW_ROOT/vera3/infra/.env" \
  $(exclude_args "$VERA_EXCLUDE")

if [ "$(date +%u)" = "$FULL_DOW" ] && ! skip_db vera; then
  WEEKLY="$DEST/vera/weekly/$TODAY"
  mkdir -p "$WEEKLY"
  echo "[$(date)] weekly vera dump -> $WEEKLY"
  # shellcheck disable=SC2046
  dump vera3-postgres vera vera "$WEEKLY" $(exclude_args "$VERA_EXCLUDE")
  ( cd "$WEEKLY" && sha256sum ./*.dump > SHA256SUMS )
fi

# Секреты внутри — только root на запись, группа NAS-пула читает.
chgrp -R "$BACKUP_GROUP" "$DEST" 2>/dev/null || true
chmod -R g+rX,o-rwx "$DEST"

for p in aibroker stepan2 vera; do
  find "$DEST/$p/daily"  -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_DAILY_DAYS"  -exec rm -rf {} \; 2>/dev/null || true
  find "$DEST/$p/weekly" -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_WEEKLY_DAYS" -exec rm -rf {} \; 2>/dev/null || true
done
echo "[$(date)] done: total=$(du -sh "$DEST" | cut -f1)"

#!/usr/bin/env bash
# Nightly backup of all project DBs + their .env secrets.
#
# Layout:  $DEST/daily/YYYY-MM-DD/   — все БД, vera БЕЗ event_embeddings
#          $DEST/weekly/YYYY-MM-DD/  — полный vera.dump (день FULL_DOW)
# event_embeddings — 80% дампа и производные данные (пересчитываемы
# доэмбеддингом хвоста): ежедневно их таскать незачем, недельной копии
# достаточно. Это держит рост бэкапов линейным по событиям, не по векторам.
#
# NAS забирает всё дерево по rrsync-only пользователю verabackup (read-only,
# см. docs/deploy-ops.md «Backups → Synology NAS»).
#
# Restore DB : docker exec -i <container> pg_restore -c -U <user> -d <db> < file.dump
# Restore env: tar xzf secrets-env.tar.gz -C /
set -euo pipefail

DEST=${DEST:-/var/backups/vera}
KEEP_DAILY_DAYS=${KEEP_DAILY_DAYS:-5}
KEEP_WEEKLY_DAYS=${KEEP_WEEKLY_DAYS:-28}
FULL_DOW=${FULL_DOW:-7}                    # ISO day-of-week: 7 = воскресенье
BACKUP_GROUP=${BACKUP_GROUP:-verabackup}   # NAS-пользователь читает через группу
# Таблицы, чьи ДАННЫЕ не входят в ежедневный дамп (схема остаётся).
VERA_DAILY_EXCLUDE=${VERA_DAILY_EXCLUDE:-event_embeddings}

TODAY=$(date +%F)
DAILY="$DEST/daily/$TODAY"
WEEKLY="$DEST/weekly/$TODAY"
mkdir -p "$DAILY"

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

exclude_args() {  # csv table list → --exclude-table-data flags
  local IFS=,
  for t in $1; do printf -- '--exclude-table-data=%s ' "$t"; done
}

echo "[$(date)] backup -> $DAILY"
dump aibroker-postgres aibroker aibroker "$DAILY"
dump stepan-postgres   stepan   stepan   "$DAILY"
dump stepan2-postgres  stepan2  stepan2  "$DAILY"
# shellcheck disable=SC2046
dump vera3-postgres    vera     vera     "$DAILY" $(exclude_args "$VERA_DAILY_EXCLUDE")

if [ "$(date +%u)" = "$FULL_DOW" ]; then
  mkdir -p "$WEEKLY"
  echo "[$(date)] weekly FULL vera dump -> $WEEKLY"
  dump vera3-postgres vera vera "$WEEKLY"
fi

# Secrets: .env holds TOKEN_SECRET — without it the encrypted tokens in the
# dumps are unrecoverable garbage. Always keep them together.
tar -czf "$DAILY/secrets-env.tar.gz" -C / \
  var/www/aibroker/.env \
  var/www/stepan/infra/.env \
  var/www/vera3/infra/.env \
  var/www/stepan2/infra/.env 2>/dev/null && echo "  secrets tarred"

( cd "$DAILY" && sha256sum ./*.dump ./*.tar.gz > SHA256SUMS )
[ -d "$WEEKLY" ] && ( cd "$WEEKLY" && sha256sum ./*.dump > SHA256SUMS )

# Секреты внутри — только root на запись, группа NAS-пула читает.
chgrp -R "$BACKUP_GROUP" "$DEST" 2>/dev/null || true
chmod -R g+rX,o-rwx "$DEST"

find "$DEST/daily"  -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_DAILY_DAYS"  -exec rm -rf {} \;
find "$DEST/weekly" -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_WEEKLY_DAYS" -exec rm -rf {} \; 2>/dev/null || true
echo "[$(date)] done: daily=$(du -sh "$DAILY" | cut -f1) total=$(du -sh "$DEST" | cut -f1)"

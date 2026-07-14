#!/usr/bin/env bash
# Nightly backup of all project DBs + their .env secrets — per-project layout:
#
#   $DEST/<project>/daily/YYYY-MM-DD/<db>.dump + secrets.tar.gz + SHA256SUMS
#   $DEST/vera/weekly/YYYY-MM-DD/vera.dump   (полный, с эмбеддингами, FULL_DOW)
#
# Сервер — только КОРОТКИЙ буфер (KEEP_DAILY_DAYS=2): длинную историю хранит
# Synology NAS, который каждую ночь забирает всё дерево read-only rrsync-юзером
# `verabackup` (см. docs/deploy-ops.md «Backups → Synology NAS»). Дневной дамп
# vera идёт без event_embeddings (80% объёма, производные данные) — полный
# срез раз в неделю.
#
# Restore DB : docker exec -i <container> pg_restore -c -U <user> -d <db> < file.dump
# Restore env: tar xzf secrets.tar.gz -C /
set -euo pipefail

DEST=${DEST:-/var/backups/vera}            # rrsync-root NAS-юзера — не менять
KEEP_DAILY_DAYS=${KEEP_DAILY_DAYS:-2}
KEEP_WEEKLY_DAYS=${KEEP_WEEKLY_DAYS:-7}
FULL_DOW=${FULL_DOW:-7}                    # ISO day-of-week: 7 = воскресенье
BACKUP_GROUP=${BACKUP_GROUP:-verabackup}   # NAS-пользователь читает через группу
VERA_DAILY_EXCLUDE=${VERA_DAILY_EXCLUDE:-event_embeddings}

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

exclude_args() {  # csv table list → --exclude-table-data flags
  local IFS=,
  for t in $1; do printf -- '--exclude-table-data=%s ' "$t"; done
}

backup_project() {  # project container db user env_path [pg_dump extra...]
  local project=$1 container=$2 db=$3 user=$4 env_path=$5; shift 5
  local out="$DEST/$project/daily/$TODAY"
  mkdir -p "$out"
  dump "$container" "$db" "$user" "$out" "$@"
  # Секреты проекта: без TOKEN_SECRET зашифрованные токены в дампе — мусор.
  if [ -f "$env_path" ]; then
    tar -czf "$out/secrets.tar.gz" -C / "${env_path#/}" \
      && echo "  secrets tarred ($project)"
  fi
  ( cd "$out" && sha256sum ./* > SHA256SUMS ) 2>/dev/null || true
}

echo "[$(date)] backup -> $DEST (per-project)"
backup_project aibroker aibroker-postgres aibroker aibroker /var/www/aibroker/.env
backup_project stepan   stepan-postgres   stepan   stepan   /var/www/stepan/infra/.env
backup_project stepan2  stepan2-postgres  stepan2  stepan2  /var/www/stepan2/infra/.env
# shellcheck disable=SC2046
backup_project vera     vera3-postgres    vera     vera     /var/www/vera3/infra/.env \
  $(exclude_args "$VERA_DAILY_EXCLUDE")

if [ "$(date +%u)" = "$FULL_DOW" ]; then
  WEEKLY="$DEST/vera/weekly/$TODAY"
  mkdir -p "$WEEKLY"
  echo "[$(date)] weekly FULL vera dump -> $WEEKLY"
  dump vera3-postgres vera vera "$WEEKLY"
  ( cd "$WEEKLY" && sha256sum ./*.dump > SHA256SUMS )
fi

# Секреты внутри — только root на запись, группа NAS-пула читает.
chgrp -R "$BACKUP_GROUP" "$DEST" 2>/dev/null || true
chmod -R g+rX,o-rwx "$DEST"

for p in aibroker stepan stepan2 vera; do
  find "$DEST/$p/daily"  -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_DAILY_DAYS"  -exec rm -rf {} \; 2>/dev/null || true
  find "$DEST/$p/weekly" -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_WEEKLY_DAYS" -exec rm -rf {} \; 2>/dev/null || true
done
echo "[$(date)] done: total=$(du -sh "$DEST" | cut -f1)"

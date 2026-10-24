#!/usr/bin/env bash
# Nightly backup of project .env secrets (+ DB dumps for projects not in SKIP_DB_PROJECTS) — per-project layout:
#
#   $DEST/<project>/daily/YYYY-MM-DD/<db>.dump + secrets.tar.gz + SHA256SUMS
#   (проекты из SKIP_DB_PROJECTS — без <db>.dump; weekly vera.dump при FULL_DOW
#   создаётся только если vera не в SKIP_DB_PROJECTS: $DEST/vera/weekly/YYYY-MM-DD/)
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
# Дампы БД всех четырёх проектов (vera, aibroker, stepan2, sniffer; sniffer этот
# скрипт никогда не бэкапил — у него был свой cron) идут только через NAS-поток (forced command `stream <project>`: Vera 02:00, Broker 02:15,
# Stepan2 02:30, Sniffer 02:45 VN). С 2026-10-23 это Vera, с 2026-10-24 —
# aibroker и stepan2: SKIP_DB_PROJECTS="vera aibroker stepan2" (дефолт).
# Сервер хранит только архивы конфигов: secrets.tar.gz + SHA256SUMS каждого
# проекта — поток их не несёт. Вернуть все дампы: SKIP_DB_PROJECTS= (пусто).
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
# Проекты (через запятую и/или пробел), чьи дампы БД здесь НЕ делаются (секреты и SHA256SUMS — делаются).
SKIP_DB_PROJECTS=${SKIP_DB_PROJECTS-vera aibroker stepan2}
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
  local -a items=()
  local p
  IFS=', ' read -ra items <<< "$SKIP_DB_PROJECTS" || true   # без glob-раскрытия
  for p in "${items[@]}"; do [ "$p" = "$1" ] && return 0; done
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
    echo "  db dump of $project skipped (SKIP_DB_PROJECTS; DB goes to NAS stream)"
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

# Удерживаемый архив: точный список файлов в манифесте (формат и команды —
# vera-retained-manifest.sh, docs/deploy-ops.md «Удерживаемый архив»). Датированный
# каталог, в котором лежит хоть один файл манифеста, ротация не трогает.
# Защищённый режим RETAINED_REQUIRED=1 (ДЕФОЛТ): манифест обязан быть. Нет файла,
# нет утилиты, он невалиден или не взялась блокировка -> за этот прогон НИЧЕГО не
# удаляется и в лог идёт ERROR; тихого отката к старой ротации нет. Старое
# поведение — только явным RETAINED_REQUIRED=0 (хосты без архива).
# Гонка: ротация держит блокировку RETAINED_LOCK (её же берёт `install` манифеста),
# читает манифест ОДИН раз в снимок и непосредственно перед каждым rm сверяет, что
# живой манифест всё ещё тот же (sha256) и каталог по-прежнему не держится; иначе
# каталог пропускается. Окно между последней сверкой и rm — микросекунды и только
# для писателя, обходящего блокировку.
# HOLD_DATES — временная страховка поверх манифеста: каталог с такой датой в имени
# не удаляется; живёт весь переходный период, убирается отдельным изменением.
RETAINED_REQUIRED=${RETAINED_REQUIRED:-1}
HOLD_DATES=${HOLD_DATES-2026-10-18 2026-10-21 2026-10-22 2026-10-23}
RETAINED_MANIFEST=${RETAINED_MANIFEST:-$DEST/RETAINED.manifest}
RETAINED_TOOL=${RETAINED_TOOL:-$WWW_ROOT/vera3/scripts/vera-retained-manifest.sh}
RETAINED_LOCK=${RETAINED_LOCK:-/var/lock/vera-retained.lock}
export RETAINED_MANIFEST RETAINED_LOCK
have_manifest=0
rotate=1
snap=""
snap_sha=""
manifest_sha() { sha256sum < "$RETAINED_MANIFEST" 2>/dev/null | cut -d' ' -f1; }
if [ -e "$RETAINED_MANIFEST" ] || [ -L "$RETAINED_MANIFEST" ]; then
  have_manifest=1
  if [ ! -f "$RETAINED_TOOL" ]; then
    rotate=0
    echo "ERROR: манифест $RETAINED_MANIFEST есть, а утилиты $RETAINED_TOOL нет — ROTATION SKIPPED, ничего не удалено"
  fi
elif [ "$RETAINED_REQUIRED" != 0 ]; then
  rotate=0
  echo "ERROR: манифест $RETAINED_MANIFEST ОТСУТСТВУЕТ при RETAINED_REQUIRED=$RETAINED_REQUIRED — ROTATION SKIPPED, ничего не удалено"
else
  echo "манифеста нет ($RETAINED_MANIFEST), RETAINED_REQUIRED=0 — ротация без защиты"
fi

if [ "$rotate" = 1 ] && { [ "$have_manifest" = 1 ] || [ "$RETAINED_REQUIRED" != 0 ]; }; then
  if ! command -v flock >/dev/null 2>&1; then
    rotate=0; echo "ERROR: нет flock — ROTATION SKIPPED, ничего не удалено"
  elif ! { exec 9>"$RETAINED_LOCK"; } 2>/dev/null || ! flock -x -w "${RETAINED_LOCK_WAIT:-60}" 9; then
    rotate=0; echo "ERROR: не взята блокировка $RETAINED_LOCK — ROTATION SKIPPED, ничего не удалено"
  fi
fi

if [ "$rotate" = 1 ] && [ "$have_manifest" = 1 ]; then
  snap=$(mktemp)
  trap 'rm -f "$snap"' EXIT
  if ! bash "$RETAINED_TOOL" validate --root "$DEST" --manifest "$RETAINED_MANIFEST" \
     || ! cp -- "$RETAINED_MANIFEST" "$snap" || ! chmod 600 "$snap"; then
    rotate=0; echo "ERROR: манифест $RETAINED_MANIFEST нечитаем/невалиден — ROTATION SKIPPED, ничего не удалено"
  else
    snap_sha=$(sha256sum < "$snap" | cut -d' ' -f1)
    if [ "$snap_sha" != "$(manifest_sha)" ] || ! bash "$RETAINED_TOOL" validate --root "$DEST" --manifest "$snap"; then
      rotate=0; echo "ERROR: манифест изменился при снятии снимка или снимок невалиден — ROTATION SKIPPED, ничего не удалено"
    fi
  fi
fi

rotate_old() {  # parent days
  local parent=$1 days=$2 d rc hd
  [ -d "$parent" ] || return 0
  while IFS= read -r -d '' d; do
    for hd in $HOLD_DATES; do
      case ${d##*/} in "$hd") echo "  удерживаю $d (HOLD_DATES)"; continue 2 ;; esac
    done
    if [ "$have_manifest" = 1 ]; then
      if bash "$RETAINED_TOOL" holds --root "$DEST" --manifest "$snap" "${d#"$DEST"/}"; then rc=0; else rc=$?; fi
      if [ "$rc" != 1 ]; then
        echo "  удерживаю $d (rc=$rc: файл манифеста внутри или снимок не прочитан)"
        continue
      fi
      # тестовый шов гонки: выполняется между решением и финальной сверкой
      [ -z "${ROTATE_RACE_HOOK:-}" ] || bash -c "$ROTATE_RACE_HOOK" _ "$d" || true
      if [ -L "$RETAINED_MANIFEST" ] || [ ! -f "$RETAINED_MANIFEST" ] || [ "$(manifest_sha)" != "$snap_sha" ]; then
        echo "  удерживаю $d (манифест изменился/пропал после решения — пропуск)"
        continue
      fi
    fi
    rm -rf -- "$d"
  done < <(find "$parent" -maxdepth 1 -type d -name '20*' -mtime +"$days" -print0 2>/dev/null || true)
}

if [ "$rotate" = 1 ]; then
  for p in aibroker stepan2 vera; do
    rotate_old "$DEST/$p/daily"  "$KEEP_DAILY_DAYS"
    rotate_old "$DEST/$p/weekly" "$KEEP_WEEKLY_DAYS"
  done
fi
echo "[$(date)] done: total=$(du -sh "$DEST" | cut -f1)"

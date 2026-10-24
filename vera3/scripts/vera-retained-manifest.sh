#!/usr/bin/env bash
# Манифест удерживаемого архива бэкапов: ТОЧНЫЙ список файлов (путь, размер,
# mtime, sha256), которые нельзя удалять ротацией и которые монитор не считает
# «накопившимся неподтверждённым» объёмом. Не cron-в-5-минут: generate/install
# запускает оператор; validate/holds/pending зовут vera-backup.sh, vera3-monitor.sh
# и vera-backup-prune.sh; verify — оператор и ежедневный cron (см. docs).
#
# Формат ($BACKUP_ROOT/RETAINED.manifest, UTF-8, LF, поля через TAB):
#   # vera-retained-manifest v1
#   # <любые комментарии, строки "# ...">
#   <sha256 hex 64><TAB><size bytes><TAB><mtime epoch, целые секунды><TAB><путь от BACKUP_ROOT>
#   # end entries=<N>
# Хвост «# end entries=N» обязателен: усечённый файл не проходит проверку.
# Любое отступление — манифест НЕВАЛИДЕН, и потребители отказывают закрыто.
# Владелец файла — root (RETAINED_TRUST_UID, дефолт 0), группа/остальные не пишут.
# Симлинк на любом участке пути от BACKUP_ROOT вниз делает манифест невалидным.
#
# Кэш проверенных файлов (RETAINED_CACHE, дефолт /var/lib/vera3-monitor/retained.verified):
# его пишет ТОЛЬКО verify после полного sha256: строка «dev:inode:size:mtime_ns:ctime_ns»
# → sha. Монитор считает файл архивом, лишь если его ТЕКУЩАЯ идентичность совпала с
# кэшем (ctime не подделать touch-ом). От подмены правами root это НЕ защищает.
#
# usage: vera-retained-manifest.sh <команда> [--root DIR] [--manifest FILE] [--cache FILE] [аргументы]
#   generate PATHS_FILE [--out FILE] [--force]  собрать манифест из явного списка путей
#   install NEW_MANIFEST [--out FILE]           под блокировкой: validate + атомарный mv
#   validate                                    0 — валиден, 2 — нет (причина в stderr)
#   verify [--no-cache]                         полная сверка с диском (sha256) + запись кэша
#   holds REL_DIR                               0 — в каталоге есть файл манифеста, 1 — нет, 2 — невалиден
#   listed REL_FILE                             0 — файл есть в манифесте, 1 — нет, 2 — невалиден
#   pending                                     объёмы для монитора: строка key=value, затем
#                                               CHANGED|UNVERIFIED|MISSING<TAB>путь
set -euo pipefail

cmd=${1:-}; [ $# -gt 0 ] && shift
root=${BACKUP_ROOT:-/var/backups/vera}
manifest=""; out=""; force=0; nocache=0; cache_arg=""; rest=()
while [ $# -gt 0 ]; do
  case $1 in
    --root) root=$2; shift 2 ;;
    --manifest) manifest=$2; shift 2 ;;
    --cache) cache_arg=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    --force) force=1; shift ;;
    --no-cache) nocache=1; shift ;;
    *) rest+=("$1"); shift ;;
  esac
done
manifest=${manifest:-${RETAINED_MANIFEST:-$root/RETAINED.manifest}}
CACHE=${cache_arg:-${RETAINED_CACHE:-/var/lib/vera3-monitor/retained.verified}}
LOCK=${RETAINED_LOCK:-/var/lock/vera-retained.lock}
TRUST_UID=${RETAINED_TRUST_UID:-0}
HEADER='# vera-retained-manifest v1'

die() { echo "vera-retained-manifest: $*" >&2; exit "${2:-2}"; }

take_lock() {  # общая с vera-backup.sh блокировка; держится до выхода
  command -v flock >/dev/null 2>&1 || die "нет flock"
  mkdir -p "$(dirname "$LOCK")" 2>/dev/null || true
  exec 9>"$LOCK" || die "не открыть $LOCK"
  flock -x -w "${RETAINED_LOCK_WAIT:-60}" 9 || die "не взял блокировку $LOCK"
}

# Ни один существующий участок пути от root вниз не должен быть симлинком.
walk_clean() {
  local cur=$root c; local -a parts
  IFS=/ read -ra parts <<< "$1"
  for c in "${parts[@]}"; do
    cur="$cur/$c"
    if [ -L "$cur" ]; then return 1; fi
    [ -e "$cur" ] || return 0
  done
  return 0
}
under_root() {  # realpath файла лежит строго под realpath корня
  local rr rp
  rr=$(realpath -m "$root"); rp=$(realpath -e "$1" 2>/dev/null) || return 1
  case $rp in "$rr"/*) return 0 ;; *) return 1 ;; esac
}
safe_file() { walk_clean "$1" && [ -f "$root/$1" ] && [ ! -L "$root/$1" ] && under_root "$root/$1"; }
ident() { find "$1" -maxdepth 0 -type f -printf '%D:%i:%s:%T@:%C@' 2>/dev/null; }
msha() { sha256sum < "$manifest" | cut -d' ' -f1; }

check_manifest() {  # 0 — валиден; иначе причина в stderr, код 2
  local f=$manifest uid mode p
  [ -e "$f" ] || [ -L "$f" ] || die "нет файла $f"
  [ -L "$f" ] && die "$f — симлинк"
  [ -f "$f" ] || die "$f — не обычный файл"
  [ -r "$f" ] || die "$f нечитаем"
  uid=$(stat -c %u "$f") || die "stat $f не удался"
  [ "$uid" = "$TRUST_UID" ] || die "$f принадлежит uid $uid, ожидается $TRUST_UID"
  mode=$(stat -c %a "$f")
  [ $(( 8#$mode & 8#022 )) -eq 0 ] || die "$f доступен на запись группе/остальным (режим $mode)"
  awk -F'\t' '
    function bad(m) { print "строка " NR ": " m > "/dev/stderr"; failed = 1; exit 1 }
    NR == 1 { if ($0 != "'"$HEADER"'") bad("нет заголовка v1"); next }
    done { bad("данные после хвоста") }
    /^# end entries=[0-9]+$/ { done = 1; want = substr($0, 15) + 0; next }
    /^# / { next }
    {
      if (NF != 4) bad("ожидается 4 поля через TAB")
      if (length($1) != 64 || $1 !~ /^[0-9a-f]+$/) bad("sha256 не 64 hex строчными")
      if ($2 !~ /^[0-9]+$/) bad("size не число")
      if ($3 !~ /^[0-9]+$/) bad("mtime не число")
      if ($4 == "" || $4 ~ /^\// || $4 ~ /(^|\/)\.\.?(\/|$)/ || $4 ~ /\/\// ) bad("недопустимый путь")
      if ($4 in seen) bad("дубль пути")
      seen[$4] = 1; n++
    }
    END {
      if (failed) exit 1
      if (NR == 0) { print "пустой файл" > "/dev/stderr"; exit 1 }
      if (!done) { print "нет хвоста # end entries=N (усечён?)" > "/dev/stderr"; exit 1 }
      if (want != n + 0) { print "хвост говорит " want ", записей " n + 0 > "/dev/stderr"; exit 1 }
    }' "$f" || die "$f невалиден"
  while IFS=$'\t' read -r _ _ _ p; do
    walk_clean "$p" || die "$f: симлинк в пути $p"
  done < <(grep -v '^#' "$f")
}

case $cmd in
  generate)
    list=${rest[0]:-}; [ -n "$list" ] && [ -f "$list" ] || die "нужен файл со списком путей" 64
    out=${out:-$manifest}
    [ "$force" = 1 ] || { [ ! -e "$out" ] && [ ! -L "$out" ]; } || die "$out уже есть (--force, чтобы заменить)" 64
    tmp="$out.tmp.$$"; errs=0; n=0; declare -A seen=()
    trap 'rm -f "$tmp"' EXIT
    { echo "$HEADER"; echo "# generated $(date -u +%FT%TZ) root=$root"; } > "$tmp"
    while IFS= read -r p || [ -n "$p" ]; do
      p=${p%$'\r'}
      case $p in ''|'#'*) continue ;; esac
      case $p in /*|*..*|*$'\t'*|*//*|./*) echo "ПЛОХОЙ ПУТЬ: $p" >&2; errs=$((errs+1)); continue ;; esac
      [ -z "${seen[$p]+x}" ] || { echo "ДУБЛЬ: $p" >&2; errs=$((errs+1)); continue; }
      seen[$p]=1
      f="$root/$p"
      if ! walk_clean "$p"; then echo "СИМЛИНК В ПУТИ: $p" >&2; errs=$((errs+1)); continue; fi
      if [ ! -f "$f" ] || [ -L "$f" ]; then echo "НЕТ ОБЫЧНОГО ФАЙЛА: $p" >&2; errs=$((errs+1)); continue; fi
      if ! under_root "$f"; then echo "ВНЕ КОРНЯ: $p" >&2; errs=$((errs+1)); continue; fi
      sum=$(sha256sum < "$f" | cut -d' ' -f1)
      printf '%s\t%s\t%s\t%s\n' "$sum" "$(stat -c %s "$f")" "$(stat -c %Y "$f")" "$p" >> "$tmp"
      n=$((n+1))
    done < "$list"
    [ "$errs" -eq 0 ] || die "ошибок: $errs — манифест не записан" 1
    echo "# end entries=$n" >> "$tmp"
    chmod 0444 "$tmp"
    mv -f "$tmp" "$out"
    [ "$(id -u)" = "$TRUST_UID" ] || echo "ВНИМАНИЕ: запущено не от uid $TRUST_UID — validate отвергнет файл (нужен владелец root)" >&2
    echo "записано $n файлов → $out"
    ;;
  install)
    new=${rest[0]:-}; [ -n "$new" ] && [ -f "$new" ] || die "нужен файл нового манифеста" 64
    target=${out:-$manifest}
    [ "$(dirname "$new")" = "$(dirname "$target")" ] || die "$new должен лежать рядом с $target (атомарный mv)" 64
    take_lock
    manifest=$new; check_manifest
    mv -f "$new" "$target"
    echo "установлен $target (sha256 $(sha256sum < "$target" | cut -d' ' -f1))"
    ;;
  validate)
    check_manifest
    ;;
  verify)
    check_manifest
    bad=0; n=0; lines=""
    while IFS=$'\t' read -r sum size mtime p; do
      n=$((n+1)); f="$root/$p"
      if ! safe_file "$p"; then echo "FAIL $p: нет обычного файла / симлинк в пути / вне корня"; bad=$((bad+1)); continue; fi
      id1=$(ident "$f"); got=$(sha256sum < "$f" | cut -d' ' -f1); id2=$(ident "$f")
      [ "$id1" = "$id2" ] || { echo "FAIL $p: изменился во время проверки"; bad=$((bad+1)); continue; }
      IFS=: read -r _ _ isz imt _ <<< "$id1"
      [ "$isz" = "$size" ] || { echo "FAIL $p: размер $isz != $size"; bad=$((bad+1)); continue; }
      [ "${imt%%.*}" = "$mtime" ] || { echo "FAIL $p: mtime ${imt%%.*} != $mtime"; bad=$((bad+1)); continue; }
      [ "$got" = "$sum" ] || { echo "FAIL $p: sha256 не совпал"; bad=$((bad+1)); continue; }
      echo "OK   $p"
      lines+="$id1"$'\t'"$sum"$'\t'"$p"$'\n'
    done < <(grep -v '^#' "$manifest")
    if [ "$nocache" = 0 ]; then
      mkdir -p "$(dirname "$CACHE")"
      { printf '# vera-retained-cache v1 manifest=%s\n' "$(msha)"; printf '%s' "$lines"; } > "$CACHE.tmp.$$"
      chmod 0644 "$CACHE.tmp.$$"; mv -f "$CACHE.tmp.$$" "$CACHE"
    fi
    echo "проверено $n, расхождений $bad"
    [ "$bad" -eq 0 ]
    ;;
  holds)
    rel=${rest[0]:-}; [ -n "$rel" ] || die "нужен REL_DIR" 64
    check_manifest
    rel=${rel%/}
    awk -F'\t' -v d="$rel/" '!/^#/ && index($4, d) == 1 { found = 1; exit } END { exit !found }' "$manifest"
    ;;
  listed)
    rel=${rest[0]:-}; [ -n "$rel" ] || die "нужен REL_FILE" 64
    check_manifest
    awk -F'\t' -v f="$rel" '!/^#/ && $4 == f { found = 1; exit } END { exit !found }' "$manifest"
    ;;
  pending)
    cache_file=/dev/null; cstate=none
    if [ -e "$manifest" ] || [ -L "$manifest" ]; then
      check_manifest; status=ok; cstate=missing
      if [ -f "$CACHE" ] && [ ! -L "$CACHE" ]; then
        cstate=stale
        if [ "$(stat -c %u "$CACHE")" = "$TRUST_UID" ] && [ $(( 8#$(stat -c %a "$CACHE") & 8#022 )) -eq 0 ] \
           && [ "$(head -n1 "$CACHE")" = "# vera-retained-cache v1 manifest=$(msha)" ]; then
          cache_file=$CACHE; cstate=ok
        fi
      fi
    else
      status=absent; manifest=/dev/null
    fi
    listing=$(find "$root" -type f -printf '%D:%i:%s:%T@:%C@\t%s\t%T@\t%P\n')
    awk -F'\t' -v status="$status" -v cstate="$cstate" '
      FILENAME == ARGV[1] {
        if ($0 !~ /^#/) { msha[$4] = $1; msize[$4] = $2; mtime[$4] = $3 }
        next
      }
      FILENAME == ARGV[2] {
        if ($0 !~ /^#/) { cid[$3] = $1; csha[$3] = $2 }
        next
      }
      {
        if ($4 == "") next
        p = $4; split($3, t, ".")
        if (!(p ~ /\.dump$/ || p ~ /\.tar\.gz$/ || (p in msize))) next
        if (p in msize) {
          seen[p] = 1
          if (msize[p] == $2 && mtime[p] == t[1]) {
            if (cid[p] == $1 && csha[p] == msha[p]) { ret += $2; nret++ }
            else { oth += $2; unv++; unvl[p] = 1 }
          } else { oth += $2; chg++; chgl[p] = 1 }
        } else oth += $2
      }
      END {
        for (p in msize) if (!(p in seen)) miss++
        printf "status=%s cache=%s retained_bytes=%d other_bytes=%d retained_files=%d changed_files=%d unverified_files=%d missing_files=%d\n",
          status, cstate, ret, oth, nret, chg, unv, miss
        for (p in chgl) printf "CHANGED\t%s\n", p
        for (p in unvl) printf "UNVERIFIED\t%s\n", p
        for (p in msize) if (!(p in seen)) printf "MISSING\t%s\n", p
      }' "$manifest" "$cache_file" <(printf '%s\n' "$listing")
    ;;
  *)
    die "команда: generate|install|validate|verify|holds|listed|pending" 64
    ;;
esac

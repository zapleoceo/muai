#!/usr/bin/env bash
# Манифест удерживаемого архива бэкапов: ТОЧНЫЙ список файлов (путь, размер,
# mtime, sha256), которые нельзя удалять ротацией и которые монитор не считает
# «накопившимся неподтверждённым» объёмом. Не cron: generate запускает оператор
# один раз; validate/holds/pending зовут vera-backup.sh и vera3-monitor.sh;
# verify — оператор, когда надо доказать, что архив цел.
#
# Формат ($BACKUP_ROOT/RETAINED.manifest, UTF-8, LF, поля через TAB):
#   # vera-retained-manifest v1
#   # <любые комментарии, строки "# ...">
#   <sha256 hex 64><TAB><size bytes><TAB><mtime epoch, целые секунды><TAB><путь от BACKUP_ROOT>
#   # end entries=<N>
# Хвост «# end entries=N» обязателен: усечённый файл не проходит проверку.
# Любое отступление (заголовок, поле, дубль пути, "..", абсолютный путь, N) —
# манифест НЕВАЛИДЕН, и потребители отказывают закрыто (ротация ничего не
# удаляет, монитор шлёт «неизвестно», а не «всё в порядке»).
# Владелец файла — root (RETAINED_TRUST_UID, дефолт 0), группа/остальные не пишут.
#
# usage: vera-retained-manifest.sh <команда> [--root DIR] [--manifest FILE] [аргументы]
#   generate PATHS_FILE [--out FILE] [--force]  собрать манифест из явного списка путей
#   validate                                    0 — валиден, 2 — нет (причина в stderr)
#   verify                                      полная сверка с диском, включая sha256
#   holds REL_DIR                               0 — в каталоге есть файл манифеста,
#                                               1 — нет, 2 — манифест невалиден
#   pending                                     объёмы для монитора: строка key=value, затем
#                                               строки CHANGED<TAB>путь / MISSING<TAB>путь
set -euo pipefail

cmd=${1:-}; [ $# -gt 0 ] && shift
root=${BACKUP_ROOT:-/var/backups/vera}
manifest=""; out=""; force=0; rest=()
while [ $# -gt 0 ]; do
  case $1 in
    --root) root=$2; shift 2 ;;
    --manifest) manifest=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    --force) force=1; shift ;;
    *) rest+=("$1"); shift ;;
  esac
done
manifest=${manifest:-${RETAINED_MANIFEST:-$root/RETAINED.manifest}}
TRUST_UID=${RETAINED_TRUST_UID:-0}
HEADER='# vera-retained-manifest v1'

die() { echo "vera-retained-manifest: $*" >&2; exit "${2:-2}"; }

check_manifest() {  # 0 — валиден; иначе причина в stderr, код 2
  local f=$manifest uid mode
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
      if [ ! -f "$f" ] || [ -L "$f" ]; then echo "НЕТ ОБЫЧНОГО ФАЙЛА: $p" >&2; errs=$((errs+1)); continue; fi
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
  validate)
    check_manifest
    ;;
  verify)
    check_manifest
    bad=0; n=0
    while IFS=$'\t' read -r sum size mtime p; do
      n=$((n+1)); f="$root/$p"
      if [ ! -f "$f" ]; then echo "FAIL $p: файла нет"; bad=$((bad+1)); continue; fi
      [ "$(stat -c %s "$f")" = "$size" ] || { echo "FAIL $p: размер $(stat -c %s "$f") != $size"; bad=$((bad+1)); continue; }
      [ "$(stat -c %Y "$f")" = "$mtime" ] || { echo "FAIL $p: mtime $(stat -c %Y "$f") != $mtime"; bad=$((bad+1)); continue; }
      [ "$(sha256sum < "$f" | cut -d' ' -f1)" = "$sum" ] || { echo "FAIL $p: sha256 не совпал"; bad=$((bad+1)); continue; }
      echo "OK   $p"
    done < <(grep -v '^#' "$manifest")
    echo "проверено $n, расхождений $bad"
    [ "$bad" -eq 0 ]
    ;;
  holds)
    rel=${rest[0]:-}; [ -n "$rel" ] || die "нужен REL_DIR" 64
    check_manifest
    rel=${rel%/}
    awk -F'\t' -v d="$rel/" '!/^#/ && index($4, d) == 1 { found = 1; exit } END { exit !found }' "$manifest"
    ;;
  pending)
    if [ -e "$manifest" ] || [ -L "$manifest" ]; then check_manifest; status=ok; else status=absent; manifest=/dev/null; fi
    listing=$(find "$root" -type f -printf '%s\t%T@\t%P\n')
    awk -F'\t' -v status="$status" '
      FILENAME == ARGV[1] {
        if ($0 !~ /^#/) { msize[$4] = $2; mtime[$4] = $3 }
        next
      }
      {
        if ($3 == "") next
        p = $3; split($2, t, ".")
        inscope = (p ~ /\.dump$/ || p ~ /\.tar\.gz$/ || (p in msize))
        if (!inscope) next
        if ((p in msize) && msize[p] == $1 && mtime[p] == t[1]) { ret += $1; nret++ }
        else { oth += $1; if (p in msize) { chg++; chgl[p] = 1 } }
        if (p in msize) seen[p] = 1
      }
      END {
        for (p in msize) if (!(p in seen)) miss++
        printf "status=%s retained_bytes=%d other_bytes=%d retained_files=%d changed_files=%d missing_files=%d\n",
          status, ret, oth, nret, chg, miss
        for (p in chgl) printf "CHANGED\t%s\n", p
        for (p in msize) if (!(p in seen)) printf "MISSING\t%s\n", p
      }' "$manifest" <(printf '%s\n' "$listing")
    ;;
  *)
    die "команда: generate|validate|verify|holds|pending" 64
    ;;
esac

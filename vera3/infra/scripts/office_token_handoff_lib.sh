# Функции передачи сервисного токена office (source-ится; сам ничего не исполняет).
# Секрет принимается только со stdin и никогда не попадает в argv/stdout/stderr.
set +x

HV_MIN_LEN=32

hv_die() { printf 'ERROR: %s\n' "$*" >&2; return 1; }

# hv_gen_token FILE — создать токен один раз; повтор переиспользует файл.
hv_gen_token() {
  local f=$1 d t
  d=$(dirname "$f")
  ( umask 077; mkdir -p "$d" ) || { hv_die "mkdir $d"; return 1; }
  chmod 700 "$d" || { hv_die "chmod $d"; return 1; }
  if [ -s "$f" ]; then
    chmod 600 "$f"
    return
  fi
  t=$(openssl rand -base64 48 | tr -d '\n=+/') || { hv_die "openssl rand"; return 1; }
  [ "${#t}" -ge "$HV_MIN_LEN" ] || { hv_die "generated token too short"; return 1; }
  ( umask 077; printf '%s\n' "$t" > "$f" ) || { hv_die "write token file"; return 1; }
  chmod 600 "$f"
}

# hv_atomic_replace FILE — новое содержимое со stdin; бэкап, temp в том же каталоге, rename.
hv_atomic_replace() {
  local f=$1 ts tmp
  [ -f "$f" ] || { hv_die "no such file: $f"; return 1; }
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  cp -p "$f" "$f.bak-$ts" || { hv_die "backup failed: $f"; return 1; }
  tmp=$(mktemp "$(dirname "$f")/.hv.XXXXXX") || { rm -f "$f.bak-$ts"; hv_die "mktemp"; return 1; }
  # HV_FAIL_BEFORE_MV=1 — тестовая инъекция сбоя после записи temp, до mv
  if ! { cat > "$tmp" && chmod --reference="$f" "$tmp" && chown --reference="$f" "$tmp" \
         && [ "${HV_FAIL_BEFORE_MV:-0}" != 1 ] && mv -f "$tmp" "$f"; }; then
    rm -f "$tmp" "$f.bak-$ts"
    hv_die "atomic replace failed: $f (original untouched)"
    return 1
  fi
}

# hv_get_kv FILE KEY — значение (без кавычек) последней строки KEY=...; пусто, если нет.
hv_get_kv() {
  local f=$1 key=$2 line val=""
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in "$key="*) val=${line#"$key="} ;; esac
  done < "$f"
  val=${val%\"}; val=${val#\"}; val=${val%\'}; val=${val#\'}
  printf '%s' "$val"
}

# hv_set_kvs FILE — строки KEY=VALUE со stdin; все замены одним atomic replace.
hv_set_kvs() {
  local f=$1 pair line out="" k i matched
  local -a keys=() pairs=() seen=()
  [ -f "$f" ] || { hv_die "no such file: $f"; return 1; }
  while IFS= read -r pair || [ -n "$pair" ]; do
    [ -n "$pair" ] || continue
    keys+=("${pair%%=*}"); pairs+=("$pair"); seen+=(0)
  done
  [ "${#keys[@]}" -gt 0 ] || { hv_die "nothing to set"; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    matched=0
    for i in "${!keys[@]}"; do
      k=${keys[$i]}
      case $line in
        "$k="*)
          matched=1
          if [ "${seen[$i]}" -eq 0 ]; then out+="${pairs[$i]}"$'\n'; seen[$i]=1; fi ;;
      esac
    done
    if [ "$matched" -eq 0 ]; then out+="$line"$'\n'; fi
  done < "$f"
  for i in "${!keys[@]}"; do
    if [ "${seen[$i]}" -eq 0 ]; then out+="${pairs[$i]}"$'\n'; fi
  done
  printf '%s' "$out" | hv_atomic_replace "$f"
}

# hv_set_kv FILE KEY — значение со stdin.
hv_set_kv() {
  local val
  IFS= read -r val || [ -n "$val" ] || { hv_die "empty value for $2"; return 1; }
  printf '%s=%s\n' "$2" "$val" | hv_set_kvs "$1"
}

# hv_upsert_site ENVFILE URL — токен со stdin. rc 0 = записано, 10 = no-op, иначе ошибка (файл не меняется).
# Обе пары пишутся одним atomic replace; отличающееся существующее значение = отказ.
hv_upsert_site() {
  local f=$1 url=$2 tok cur_url cur_tok
  IFS= read -r tok || { hv_die "no token on stdin"; return 1; }
  [ "${#tok}" -ge "$HV_MIN_LEN" ] || { hv_die "token shorter than $HV_MIN_LEN"; return 1; }
  [ -f "$f" ] || { hv_die "no such file: $f"; return 1; }
  cur_url=$(hv_get_kv "$f" OFFICE_API_URL)
  cur_tok=$(hv_get_kv "$f" OFFICE_SERVICE_TOKEN)
  if [ -n "$cur_url" ] && [ "$cur_url" != "$url" ]; then
    hv_die "OFFICE_API_URL already set to a different value; refusing to overwrite"; return 1
  fi
  if [ -n "$cur_tok" ] && [ "$cur_tok" != "$tok" ]; then
    hv_die "OFFICE_SERVICE_TOKEN already set to a different value; refusing to overwrite"; return 1
  fi
  if [ -n "$cur_url" ] && [ -n "$cur_tok" ]; then echo "site env: already set (no-op)"; return 10; fi
  printf 'OFFICE_API_URL=%s\nOFFICE_SERVICE_TOKEN=%s\n' "$url" "$tok" | hv_set_kvs "$f" || return 1
  echo "site env: written"
}

# hv_token_collides FILE NAME… — yes/no: токен (переменная HV_TOK) равен полю значения NAME.
hv_token_collides() {
  local f=$1 name raw field
  local -a parts fields
  shift
  for name in "$@"; do
    raw=$(hv_get_kv "$f" "$name")
    IFS=, read -ra parts <<< "$raw" || true
    for part in "${parts[@]}"; do
      IFS=: read -ra fields <<< "$part" || true
      for field in "${fields[@]}"; do
        if [ "$field" = "$HV_TOK" ]; then echo yes; return 0; fi
      done
    done
  done
  echo no
}

# hv_upsert_registry ENVFILE NAME PROJECT — токен со stdin.
# rc 0 = добавлено, 10 = no-op, иначе ошибка (файл не меняется).
hv_upsert_registry() {
  local f=$1 name=$2 project=$3 HV_TOK reg entry part cur_tok="" found=0 coll
  local -a parts
  IFS= read -r HV_TOK || [ -n "$HV_TOK" ] || { hv_die "no token on stdin"; return 1; }
  [ "${#HV_TOK}" -ge "$HV_MIN_LEN" ] || { hv_die "token shorter than $HV_MIN_LEN"; return 1; }
  [ -f "$f" ] || { hv_die "no such file: $f"; return 1; }
  coll=$(hv_token_collides "$f" MCP_TOKENS ROOM_TOKENS)
  printf 'token collides with MCP/ROOM tokens: %s\n' "$coll"
  [ "$coll" = no ] || { hv_die "token equals an existing MCP/ROOM token"; return 1; }
  reg=$(hv_get_kv "$f" OFFICE_SERVICE_TOKENS)
  IFS=, read -ra parts <<< "$reg" || true
  for part in "${parts[@]}"; do
    if [ "${part%%:*}" = "$name" ]; then found=1; cur_tok=${part##*:}; fi
  done
  if [ "$found" -eq 1 ]; then
    if [ "$cur_tok" = "$HV_TOK" ]; then echo "registry: $name already set (no-op)"; return 10; fi
    hv_die "$name already registered with a different token; refusing to overwrite"
    return 1
  fi
  entry="$name:$project:$HV_TOK"
  if [ -n "$reg" ]; then entry="$reg,$entry"; fi
  printf '%s\n' "$entry" | hv_set_kv "$f" OFFICE_SERVICE_TOKENS || return 1
  echo "registry: $name added"
}

HV_VERA_ENV=${HV_VERA_ENV:-/var/www/vera3/infra/.env}
HV_SITE_ENV=${HV_SITE_ENV:-/var/www/veranda_my_usr/data/www/veranda.my/.env}
HV_API_URL=${HV_API_URL:-https://dima.veranda.my}

# hv_remote_vera — на сервере Vera; токен со stdin.
hv_remote_vera() {
  local tok rc=0 dir st
  IFS= read -r tok || { hv_die "no token on stdin"; return 1; }
  printf '%s\n' "$tok" | hv_upsert_registry "$HV_VERA_ENV" veranda_bot Veranda || rc=$?
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 10 ]; then return 1; fi
  dir=$(dirname "$HV_VERA_ENV")
  ( cd "$dir" && docker compose up -d --no-deps mcp </dev/null ) || { hv_die "compose up mcp"; return 1; }
  st=$( cd "$dir" && docker compose exec -T mcp sh -c '[ -n "$OFFICE_SERVICE_TOKENS" ] && echo set || echo unset' </dev/null ) \
    || { hv_die "in-container check"; return 1; }
  echo "container OFFICE_SERVICE_TOKENS: $st"
  [ "$st" = set ] || { hv_die "token registry not visible in container"; return 1; }
}

# hv_remote_site — на сервере сайта; токен со stdin.
hv_remote_site() {
  local tok code
  IFS= read -r tok || { hv_die "no token on stdin"; return 1; }
  local rc=0
  printf '%s\n' "$tok" | hv_upsert_site "$HV_SITE_ENV" "$HV_API_URL" || rc=$?
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 10 ]; then return 1; fi
  code=$(printf 'Authorization: Bearer %s\n' "$tok" | curl -s -o /dev/null -w '%{http_code}' -H @- "$HV_API_URL/office/v1/tasks") \
    || { hv_die "curl failed"; return 1; }
  echo "GET /office/v1/tasks -> HTTP $code"
  [ "$code" = 200 ] || { hv_die "expected HTTP 200"; return 1; }
}

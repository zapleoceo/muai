#!/usr/bin/env bash
# Запускает владелец из Git Bash на ноутбуке. Агенты токен и его хэш не читают.
# Токен: ~/.vera_handoff/veranda_bot.token (создаётся один раз, повтор его переиспользует).
# Порядок: реестр Vera -> перезапуск mcp -> проверка в контейнере -> .env сайта -> curl с хоста сайта.
set -euo pipefail
set +x
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=office_token_handoff_lib.sh
. "$here/office_token_handoff_lib.sh"

tokfile=${HV_TOKEN_FILE:-$HOME/.vera_handoff/veranda_bot.token}
hv_gen_token "$tokfile"
IFS= read -r tok < "$tokfile"
[ "${#tok}" -ge "$HV_MIN_LEN" ] || { echo "ERROR: token file too short" >&2; exit 1; }

# ship FUNC ssh-args… — библиотека и токен идут по stdin, не в argv
ship() {
  local func=$1; shift
  {
    cat "$here/office_token_handoff_lib.sh"
    printf '%s <<'"'"'HV_TOK_EOF'"'"'\n%s\nHV_TOK_EOF\n' "$func" "$tok"
  } | ssh "$@" 'bash -s'
}

echo "== 1/2 Vera (hetzner-root)"
ship hv_remote_vera hetzner-root \
  || { echo "FAILED at Vera step; site untouched. Rerun is safe (token reused)." >&2; exit 1; }

echo "== 2/2 site (veranda.my)"
ship hv_remote_site -i ~/.ssh/veranda_deploy_work -o IdentitiesOnly=yes veranda_my_usr@5.101.179.132 \
  || { echo "FAILED at site step; Vera already updated. Rerun is safe (token reused)." >&2; exit 1; }

echo "OK: token handed off (value not printed)"

#!/usr/bin/env bash
# Печатает базу диффа для гейтов CI. Вход — переменные окружения воркфлоу:
# CI_EVENT, CI_BEFORE, CI_PR_BASE, CI_HEAD.
# На pull_request `before` пуст, а CI_HEAD — merge-коммит PR: фолбэк HEAD~1
# взял бы верхушку базовой ветки и вывернул бы дифф (правки прочлись бы как
# удаления), поэтому база PR — pull_request.base.sha.
set -euo pipefail

HEAD="${CI_HEAD:?CI_HEAD is required}"
if [ "${CI_EVENT:-}" = "pull_request" ]; then
  BASE="${CI_PR_BASE:-}"
else
  BASE="${CI_BEFORE:-}"
fi
if [ -z "$BASE" ] || [ "$BASE" = "0000000000000000000000000000000000000000" ] \
   || ! git cat-file -e "${BASE}^{commit}" 2>/dev/null; then
  BASE="$(git rev-parse "${HEAD}~1" 2>/dev/null || echo "$HEAD")"
fi
echo "$BASE"

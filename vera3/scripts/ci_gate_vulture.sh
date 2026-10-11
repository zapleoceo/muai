#!/usr/bin/env bash
# Vulture (мёртвый код) на изменённых файлах. Единый источник для job
# `quality` (deploy.yml) и scripts/preflight.sh. VULTURE_CMD — как вызывать.
# usage: ci_gate_vulture.sh BASE HEAD
set -euo pipefail
BASE="${1:?BASE}"; HEAD="${2:?HEAD}"
CHANGED="$(git diff --name-only --diff-filter=AM "$BASE" "$HEAD" \
            | grep -E '^vera3/(services|shared)/.*\.py$' || true)"
if [ -z "$CHANGED" ]; then
  echo "No Python changes under vera3/services|shared — skip."
  exit 0
fi
echo "Scanning:"; echo "$CHANGED"
# --min-confidence 80: срабатывает при 4/5 уверенности. --ignore-names cls:
# vulture считает `cls` в @classmethod (валидаторы pydantic) неиспользуемой.
# shellcheck disable=SC2086
echo "$CHANGED" | xargs ${VULTURE_CMD:-vulture} --min-confidence 80 --ignore-names cls \
  --exclude '*/tests/*,*/__init__.py' || {
    echo "::warning::Vulture found suspected dead code — see above."
    exit 1
  }

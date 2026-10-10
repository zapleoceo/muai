#!/usr/bin/env bash
# Ruff (strict) на изменённых файлах. Единый источник для job `quality`
# (deploy.yml) и scripts/preflight.sh. RUFF_CMD — как вызывать ruff.
# usage: ci_gate_ruff.sh BASE HEAD
set -euo pipefail
BASE="${1:?BASE}"; HEAD="${2:?HEAD}"
CHANGED="$(git diff --name-only --diff-filter=AM "$BASE" "$HEAD" \
            | grep -E '^vera3/(services|shared|tests)/.*\.py$' || true)"
if [ -z "$CHANGED" ]; then
  echo "No changed .py files — skip ruff"
  exit 0
fi
echo "Linting:"; echo "$CHANGED"
# E501 исключён — форматтера в проекте нет. B008: Depends()/Query()/File() в
# дефолтах FastAPI — идиома. RUF006 — повисшие create_task. DTZ003 — только
# utcnow (DTZ001 ругался бы на datetime(2026,...) в фикстурах).
# shellcheck disable=SC2086
echo "$CHANGED" | xargs ${RUFF_CMD:-ruff} check \
  --select E,F,W,I,B,UP,SIM,C4,RET,RUF006,DTZ003 --ignore E501,E402,B008

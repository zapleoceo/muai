#!/usr/bin/env bash
# Единственное место со списком зависимостей CI. Режимы:
#   lint — только линтеры/сканеры (job quality, пакеты не нужны);
#   test — все пакеты проекта + инструменты покрытия (job test).
# Раньше список жил в пяти копиях в двух воркфлоу и расходился: vera3-tests.yml
# не ставил ingestor-gmail/slack/trello, а тесты их импортируют.
set -euo pipefail

VULTURE="vulture==2.13"
RUFF="ruff==0.7.4"
DIFF_COVER="diff-cover==9.2.0"
MYPY="mypy"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-}"

case "$MODE" in
  lint)
    pip install "$RUFF" "$VULTURE"
    ;;
  test)
    pip install -e "$ROOT/shared[dev]" aiosqlite
    pip install -e "$ROOT/services/gateway[dev]"
    for svc in dashboard media-worker ingestor-telegram ingestor-gmail \
               ingestor-trello ingestor-slack brain-search bot-telegram mcp; do
      pip install -e "$ROOT/services/$svc"
    done
    pip install "$RUFF" "$DIFF_COVER" "$MYPY"
    ;;
  *)
    echo "usage: ci_install.sh lint|test" >&2
    exit 2
    ;;
esac

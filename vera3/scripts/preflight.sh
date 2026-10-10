#!/usr/bin/env bash
# Один запуск перед push: те же гейты, что в .github/workflows/deploy.yml.
# Гейты docs / secrets / ruff / vulture / docs name-sync — это ТЕ ЖЕ файлы
# scripts/ci_gate_*.sh, которые вызывает воркфлоу, поэтому разойтись не могут.
# pytest — как в job `test`: cwd=vera3, `pytest tests/`, без ini-файла
# (rootdir=vera3 → asyncio strict; vera3/shared/pyproject.toml с mode=auto
# сюда не попадает). Добавлено --continue-on-collection-errors, чтобы одна
# ненайденная зависимость не прятала остальные падения; код возврата тот же.
#
#   bash vera3/scripts/preflight.sh [--partial] [--fast] [--base REF]
#
# Режимы:
#   по умолчанию — ГЕЙТ. RESULT: PASS только если выполнен весь обязательный
#     набор и он зелёный: все гейты, полный pytest вместе с PG-интеграцией
#     (PREFLIGHT_PG_URL), пороги покрытия, diff-cover. Чего-то не хватает
#     (нет PG URL, pytest-cov, diff-cover, ruff, vulture) → RESULT: INCOMPLETE,
#     код 3, каждый пропуск перечислен. Падение любого гейта → FAIL, код 1.
#     Не удался git fetch → блокер «stale base: fetch failed», код 1.
#   --partial — диагностика, НЕ гейт: PASS не печатается никогда, итог
#     RESULT: PARTIAL, код 0 только если выполненное зелёное.
#   --fast — pytest только по затронутым тестам; подразумевает --partial.
#   --base REF — база диффа вместо `git merge-base origin/master HEAD`.
# Env: PREFLIGHT_PY (python, по умолчанию python),
#      PREFLIGHT_PG_URL (postgresql+asyncpg://… → включает tests/pg и
#      tests/integration, как RUN_INTEGRATION_TESTS=1 в CI).
set -uo pipefail

PY="${PREFLIGHT_PY:-python}"
export PYTHONIOENCODING=utf-8  # check_coverage.py печатает кириллицу; cp1252 на Windows падает
FAST=0
PARTIAL=0
BASE_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1; PARTIAL=1 ;;
    --partial) PARTIAL=1 ;;
    --base) BASE_OVERRIDE="${2:?--base REF}"; shift ;;
    *) echo "usage: preflight.sh [--partial] [--fast] [--base REF]" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
HERE="vera3/scripts"

echo "== fetch origin/master"
if ! git fetch -q origin master; then
  if [ "$PARTIAL" -eq 0 ]; then
    echo "BLOCKER: stale base: fetch failed"
    echo "RESULT: FAIL"
    exit 1
  fi
  echo "WARN: stale base: fetch failed (partial mode continues; non-gating)"
fi
HEAD_SHA="$(git rev-parse HEAD)"
BASE="${BASE_OVERRIDE:-$(git merge-base origin/master HEAD)}"
echo "base=$BASE"
echo "head=$HEAD_SHA"

[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || echo "WARN: есть незакоммиченные правки отслеживаемых файлов — гейты смотрят на коммиты (BASE..HEAD), CI тоже"

RUFF_CMD="$PY -m ruff"; export RUFF_CMD
VULTURE_CMD="$PY -m vulture"; export VULTURE_CMD

declare -a NAMES=() RESULTS=() MISSING=()
FAILED=0
record() { NAMES+=("$1"); RESULTS+=("$2"); [ "$2" = FAIL ] && FAILED=1; return 0; }
# Чего-то не хватает: в гейте это INCOMPLETE, в --partial — видимый SKIP.
absent() {
  if [ "$PARTIAL" -eq 1 ]; then record "$1" "SKIP($2)"
  else record "$1" "MISSING($2)"; MISSING+=("$1: $2"); fi
}

run_gate() {
  local name="$1"; shift
  echo; echo "=== $name"
  if "$@"; then record "$name" PASS; else record "$name" FAIL; fi
}

# Инструменты, без которых набор неполон (CI ставит их через ci_install.sh).
HAVE_COV=0;  "$PY" -c "import pytest_cov" 2>/dev/null && HAVE_COV=1
HAVE_DC=0;   "$PY" -m diff_cover.diff_cover_tool --help >/dev/null 2>&1 && HAVE_DC=1
HAVE_RUFF=0; "$PY" -m ruff --version >/dev/null 2>&1 && HAVE_RUFF=1
HAVE_VUL=0;  "$PY" -m vulture --version >/dev/null 2>&1 && HAVE_VUL=1

run_gate "docs gate"       bash "$HERE/ci_gate_docs.sh" "$BASE" "$HEAD_SHA"
run_gate "secret scan"     bash "$HERE/ci_gate_secrets.sh" "$BASE" "$HEAD_SHA"
if [ "$HAVE_RUFF" -eq 1 ]; then
  run_gate "ruff (strict)" bash "$HERE/ci_gate_ruff.sh" "$BASE" "$HEAD_SHA"
else absent "ruff (strict)" "ruff не установлен"; fi
if [ "$HAVE_VUL" -eq 1 ]; then
  run_gate "vulture"       bash "$HERE/ci_gate_vulture.sh" "$BASE" "$HEAD_SHA"
else absent "vulture" "vulture не установлен"; fi
run_gate "docs name-sync"  bash "$HERE/ci_gate_docs_namesync.sh" "$BASE" "$HEAD_SHA"

# ── pytest ──────────────────────────────────────────────────────────────────
export TELEGRAM_API_ID=1 TELEGRAM_API_HASH=test TELEGRAM_PHONE=+10000000000
if [ -n "${PREFLIGHT_PG_URL:-}" ]; then
  export RUN_INTEGRATION_TESTS=1 TEST_DATABASE_URL="$PREFLIGHT_PG_URL"
  record "PG integration" "RUN"
else
  unset RUN_INTEGRATION_TESTS TEST_DATABASE_URL
  if [ "$PARTIAL" -eq 1 ]; then
    echo; echo "PG integration: not run — CI authoritative"
    record "PG integration" "not run — CI authoritative"
  else
    absent "PG integration" "PREFLIGHT_PG_URL не задан"
  fi
fi

echo; echo "=== pytest"
COV_ARGS=()
if [ "$FAST" -eq 1 ]; then
  TARGETS=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ -e "$f" ] && TARGETS+=("${f#vera3/}")
  done < <(git diff --name-only --diff-filter=AM "$BASE" "$HEAD_SHA" \
             | grep -E '^vera3/tests/.*test_[^/]*\.py$' || true)
  while IFS= read -r src; do
    [ -z "$src" ] && continue
    mod="$(basename "$src" .py)"
    while IFS= read -r t; do
      [ -n "$t" ] && TARGETS+=("${t#vera3/}")
    done < <(grep -rlw --include='test_*.py' -- "$mod" vera3/tests 2>/dev/null || true)
  done < <(git diff --name-only --diff-filter=AM "$BASE" "$HEAD_SHA" \
             | grep -E '^vera3/(services|shared)/.*\.py$' | grep -v '__init__' || true)
  mapfile -t TARGETS < <(printf '%s\n' "${TARGETS[@]}" | sort -u | grep -v '^$' || true)
  if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "--fast: нет затронутых тестов"
    record "pytest (--fast)" "SKIP(нет тестов под изменения)"
  else
    printf 'fast targets:\n'; printf '  %s\n' "${TARGETS[@]}"
    if (cd vera3 && "$PY" -m pytest -o asyncio_mode=strict --continue-on-collection-errors "${TARGETS[@]}"); then
      record "pytest (--fast)" PASS
    else
      record "pytest (--fast)" FAIL
    fi
  fi
  record "coverage floors" "SKIP(--fast)"
  record "diff-cover >=75%" "SKIP(--fast)"
else
  if [ "$HAVE_COV" -eq 1 ]; then
    COV_ARGS=(--cov=vera_shared --cov=gateway --cov=media_worker
      --cov=brain_search --cov=brain_triage --cov=dashboard
      --cov=ingestor_gmail --cov=ingestor_slack --cov=ingestor_trello
      --cov=ingestor_telegram --cov=bot_telegram --cov=vera_mcp
      --cov-report=term-missing --cov-report=xml:coverage.xml)
  fi
  rm -f vera3/coverage.xml
  if (cd vera3 && "$PY" -m pytest -o asyncio_mode=strict --continue-on-collection-errors tests/ "${COV_ARGS[@]}"); then
    record "pytest" PASS
  else
    record "pytest" FAIL
  fi

  if [ "$HAVE_COV" -eq 1 ] && [ -f vera3/coverage.xml ]; then
    echo; echo "=== coverage floors"
    if (cd vera3 && "$PY" scripts/check_coverage.py coverage.xml); then
      record "coverage floors" PASS
    else
      record "coverage floors" FAIL
    fi
  else
    absent "coverage floors" "нет pytest-cov или coverage.xml"
  fi

  echo; echo "=== diff-cover"
  if [ "$HAVE_DC" -eq 0 ]; then
    echo "diff-cover не установлен: pip install diff-cover==9.2.0"
    absent "diff-cover >=75%" "diff-cover не установлен"
  elif ! git diff --name-only "$BASE" "$HEAD_SHA" | grep -qE '^vera3/.*\.py$'; then
    echo "No .py changes — skip diff-cover (как в CI)"
    record "diff-cover >=75%" "PASS(нет .py в диффе)"
  elif [ ! -f vera3/coverage.xml ]; then
    absent "diff-cover >=75%" "нет coverage.xml"
  elif (cd vera3 \
        && "$PY" -m coverage xml \
             --include='*/shared/vera_shared/*,*/services/gateway/*,*/services/media-worker/*,*/services/brain-search/*,*/services/mcp/*' \
             -o coverage-diffcover.xml \
        && "$PY" -m diff_cover.diff_cover_tool coverage-diffcover.xml \
             --compare-branch="$BASE" --fail-under=75); then
    record "diff-cover >=75%" PASS
  else
    record "diff-cover >=75%" FAIL
  fi
fi

# ── итог ────────────────────────────────────────────────────────────────────
echo; echo "================ PREFLIGHT SUMMARY ================"
MODE="gating"; [ "$PARTIAL" -eq 1 ] && MODE="partial, non-gating"
echo "base=${BASE:0:10} head=${HEAD_SHA:0:10} mode=$MODE fast=$FAST py=$PY"
for i in "${!NAMES[@]}"; do printf '%-20s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}"; done
if [ "$FAILED" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
if [ "$PARTIAL" -eq 1 ]; then
  echo "RESULT: PARTIAL (non-gating: выполненное зелёное, но это не допуск к push; CI авторитетен)"
  exit 0
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "RESULT: INCOMPLETE — обязательный набор выполнен не полностью:"
  printf '  - %s\n' "${MISSING[@]}"
  exit 3
fi
echo "RESULT: PASS"
exit 0

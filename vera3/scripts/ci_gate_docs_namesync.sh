#!/usr/bin/env bash
# Docs name-sync: добавленный публичный символ обязан упоминаться в
# vera3/docs/, удалённый — не должен. Единый источник для job `quality`
# (deploy.yml) и scripts/preflight.sh.
# usage: ci_gate_docs_namesync.sh BASE HEAD
set -euo pipefail
BASE="${1:?BASE}"; HEAD="${2:?HEAD}"
if git log --format=%B "${BASE}..${HEAD}" 2>/dev/null | grep -qiF "docs-not-needed"; then
  echo "::notice::'docs-not-needed' marker — skip docs name-sync"
  exit 0
fi
# Публичные имена = lowercase def + PascalCase class; без _private, test_,
# dunder и общих имён (main/run/... и `Source` — обычные слова в доках,
# дают ложные срабатывания при добавлении/удалении такого файла).
SKIP='^test_|^_|^(main|run|serve|create_app|lifespan|healthz|startup|shutdown|Source)$'
NAME_RE='^[+-](async )?def [a-z][a-z0-9_]*\(|^[+-]class [A-Z][A-Za-z0-9_]*[\(:]'
DIFF="$(git diff "$BASE" "$HEAD" -- 'vera3/services/**/*.py' 'vera3/shared/**/*.py' || true)"
ADDED="$(echo "$DIFF" | grep -E "^\+" | grep -E "$NAME_RE" \
          | sed -E 's/^\+(async )?def ([a-z][a-z0-9_]*)\(.*/\2/;
                    s/^\+class ([A-Z][A-Za-z0-9_]*).*/\1/' \
          | grep -vE "$SKIP" | sort -u || true)"
REMOVED="$(echo "$DIFF" | grep -E "^-" | grep -E "$NAME_RE" \
            | sed -E 's/^-(async )?def ([a-z][a-z0-9_]*)\(.*/\2/;
                      s/^-class ([A-Z][A-Za-z0-9_]*).*/\1/' \
            | grep -vE "$SKIP" | sort -u || true)"
NET_ADDED="$(comm -23 <(echo "$ADDED") <(echo "$REMOVED") | grep -v '^$' || true)"
NET_REMOVED="$(comm -23 <(echo "$REMOVED") <(echo "$ADDED") | grep -v '^$' || true)"
fail=0
if [ -n "$NET_ADDED" ]; then
  echo "Added public names: $NET_ADDED"
  for name in $NET_ADDED; do
    # По границам слова: удаление `Foo` не «находится» в доке с `FooBar`.
    if ! grep -rqE -- "\b$name\b" vera3/docs/ 2>/dev/null; then
      echo "::error::Added public symbol '$name' not mentioned anywhere in vera3/docs/"
      fail=1
    fi
  done
fi
if [ -n "$NET_REMOVED" ]; then
  echo "Removed public names: $NET_REMOVED"
  for name in $NET_REMOVED; do
    if grep -rqE -- "\b$name\b" vera3/docs/ 2>/dev/null; then
      echo "::error::Removed symbol '$name' still referenced in vera3/docs/ (orphan)"
      fail=1
    fi
  done
fi
[ "$fail" -eq 0 ] && echo "docs name-sync passed"
exit $fail

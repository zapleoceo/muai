#!/usr/bin/env bash
# Docs-гейт: код в vera3/services|shared без правки vera3/docs блокирует пуш.
# Единый источник для job `docs` (deploy.yml) и scripts/preflight.sh.
# usage: ci_gate_docs.sh BASE HEAD
set -euo pipefail
BASE="${1:?BASE}"; HEAD="${2:?HEAD}"
CHANGED="$(git diff --name-only "$BASE" "$HEAD")"
echo "changed files:"; echo "$CHANGED"

if git log --format=%B "${BASE}..${HEAD}" 2>/dev/null | grep -qiF "docs-not-needed"; then
  echo "::notice::'docs-not-needed' marker found, skipping docs gate"
  exit 0
fi

CODE="$(echo "$CHANGED" | grep -E '^vera3/(services|shared)/' || true)"
DOCS="$(echo "$CHANGED" | grep -E '^vera3/docs/' || true)"
if [ -n "$CODE" ] && [ -z "$DOCS" ]; then
  echo "::error::Code changed in vera3/services or vera3/shared but vera3/docs was not updated."
  echo "Update the relevant doc OR add 'docs-not-needed' to a commit message."
  exit 1
fi
echo "docs gate passed"

#!/usr/bin/env bash
# Secret scan по ДОБАВЛЕННЫМ строкам диффа (история хранит давно отозванный
# токен, полный скан падал бы вечно). Единый источник для job `quality`
# (deploy.yml) и scripts/preflight.sh.
# usage: ci_gate_secrets.sh BASE HEAD
set -euo pipefail
BASE="${1:?BASE}"; HEAD="${2:?HEAD}"
ADDED="$(git diff "$BASE" "$HEAD" | grep -E '^\+' | grep -v '^\+\+\+' || true)"
PATTERNS='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}'
if HITS="$(echo "$ADDED" | grep -nEo "$PATTERNS" | sed -E 's/(.{12}).*/\1…<redacted>/')" && [ -n "$HITS" ]; then
  echo "::error::A credential appears in this diff — do NOT push it."
  echo "$HITS"
  echo "Revoke the exposed credential first, then remove it from the change."
  exit 1
fi
echo "✓ no credentials in the diff"

#!/bin/bash
# Automated test — 10 разнообразных вопросов Вере 3.0 через search API
# Запуск: ssh hetzner-root bash /var/www/vera3/scripts/test_10_questions.sh

set -u

ask() {
  local Q="$1"
  echo ""
  echo "=========================================="
  echo "Q: $Q"
  echo "=========================================="
  R=$(curl -s -X POST http://localhost:8002/search \
       -H 'Content-Type: application/json' \
       -d "$(jq -n --arg q "$Q" '{q:$q, limit:15}')" 2>&1)
  echo "$R" | jq -r '.answer // "(no answer)"' 2>/dev/null || echo "$R" | head -c 500
  echo ""
  echo "Provider: $(echo "$R" | jq -r '.provider // "?"' 2>/dev/null)"
  echo "Cost: \$$(echo "$R" | jq -r '.cost_usd // 0' 2>/dev/null)"
  echo "Results: $(echo "$R" | jq -r '.results | length' 2>/dev/null) events"
  sleep 2
}

ask "кто такой Дмитрий Егоров"
ask "что было с переездом в Индонезию"
ask "что Маша мне писала в последнее время"
ask "какие были обсуждения по Veranda бару"
ask "что я писал про KPI"
ask "что было с визой Джакарта"
ask "какие у меня были встречи в июне"
ask "что я писал про лизу"
ask "какие были проблемы с deploy"
ask "какие пришли новости от ITStep на этой неделе"

echo ""
echo "=========================================="
echo "Test complete"
echo "=========================================="

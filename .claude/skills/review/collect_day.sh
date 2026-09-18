#!/usr/bin/env bash
# Сбор материала за рабочий день для ревью.
#
# Зачем: 2026-09-01 ревью потеряло главное по Оркестре, потому что выгрузка резала
# Slack на 175 знаков, а три сообщения Виктора были на 2300, 1818 и 1808. По обрезку
# получился вялый пересказ первой строки и неверный вывод «показывать нечего».
# Скрипт закрывает это механически: короткие сообщения идут списком, длинные —
# целиком, и отдельно печатается свежесть каждого источника.
#
# Авторство печатается направлением «АВТОР --> [чат]», а не «[чат] АВТОР:». Название
# чата вплотную к тексту четыре раза превращалось в прозе в автора («Алексей прислал»
# про сообщение Димы в чате Алексея). Направление эту ошибку не пропускает.
#
# Использование:
#   bash collect_day.sh 2026-09-01 > day.txt
#
# Местный день (GMT+7) = UTC с 17:00 предыдущих суток до 17:00 текущих.

set -euo pipefail

DAY="${1:-}"
if [[ -z "$DAY" ]]; then
  echo "укажи дату: bash collect_day.sh 2026-09-01" >&2
  exit 2
fi

PREV=$(date -d "$DAY -1 day" +%Y-%m-%d)
FROM="$PREV 17:00"
TO="$DAY 17:00"
LONG=400   # порог, выше которого сообщение печатается целиком

psql() { ssh hetzner-root "docker exec -i vera3-postgres psql -U vera -d vera -t -A -F'|'"; }

echo "================ ДЕНЬ $DAY (окно UTC $FROM .. $TO) ================"
echo
echo "--- СВЕЖЕСТЬ ИНГЕСТА (если источник отстал, об этом надо написать в ревью) ---"
psql <<SQL
SELECT source, to_char(max(occurred_at) + interval '7 hours', 'MM-DD HH24:MI') AS last_local
FROM events GROUP BY source ORDER BY 1;
SQL

echo
echo "--- ОБЪЁМ ПО ИСТОЧНИКАМ ---"
psql <<SQL
SELECT source, coalesce(category,'-') AS cat, count(*),
       to_char(min(occurred_at) + interval '7 hours','HH24:MI') AS first_local,
       to_char(max(occurred_at) + interval '7 hours','HH24:MI') AS last_local
FROM events WHERE occurred_at >= '$FROM' AND occurred_at < '$TO'
GROUP BY 1,2 ORDER BY 3 DESC;
SQL

echo
echo "--- РАБОЧИЕ СЕССИИ (целиком, это самый плотный материал) ---"
psql <<SQL
SELECT to_char(occurred_at + interval '7 hours','HH24:MI') || E'\n' ||
       regexp_replace(content_text, '[\r\n]+', ' | ', 'g')
FROM events WHERE source='claude_chat' AND category='session'
  AND occurred_at >= '$FROM' AND occurred_at < '$TO' ORDER BY occurred_at;
SQL

echo
echo "--- СОЗВОНЫ (с длительностью, целиком) ---"
psql <<SQL
SELECT to_char(occurred_at + interval '7 hours','HH24:MI') || '  ' ||
       coalesce(metadata->>'duration_s','?') || ' сек' || E'\n' ||
       regexp_replace(content_text, '[\r\n]+', ' | ', 'g')
FROM events WHERE source='voice'
  AND occurred_at >= '$FROM' AND occurred_at < '$TO' ORDER BY occurred_at;
SQL

echo
echo "--- ПЕРЕПИСКА: КОРОТКИЕ (до $LONG знаков) ---"
psql <<SQL
SELECT to_char(occurred_at + interval '7 hours','HH24:MI') || '  ' ||
       CASE WHEN metadata->>'author_role'='self' THEN 'ДИМА'
            ELSE coalesce(nullif(metadata->>'author_label',''),'?') END || ' --> [' ||
       coalesce(metadata->>'channel_name', metadata->>'chat_title','?') || ']: ' ||
       regexp_replace(content_text, '[\r\n]+', ' ', 'g')
FROM events
WHERE source IN ('slack','telegram') AND occurred_at >= '$FROM' AND occurred_at < '$TO'
  AND length(content_text) <= $LONG
ORDER BY occurred_at;
SQL

echo
echo "--- ПЕРЕПИСКА: ДЛИННЫЕ (от $LONG знаков, ЦЕЛИКОМ, не пересказывать по первой строке) ---"
psql <<SQL
SELECT E'\n===== ' || to_char(occurred_at + interval '7 hours','HH24:MI') || '  ' ||
       CASE WHEN metadata->>'author_role'='self' THEN 'ДИМА'
            ELSE coalesce(nullif(metadata->>'author_label',''),'?') END || ' --> [' ||
       coalesce(metadata->>'channel_name', metadata->>'chat_title','?') || ']' ||
       ' (' || length(content_text) || ' знаков) =====' || E'\n' || content_text
FROM events
WHERE source IN ('slack','telegram') AND occurred_at >= '$FROM' AND occurred_at < '$TO'
  AND length(content_text) > $LONG
ORDER BY occurred_at;
SQL

echo
echo "--- ПОЧТА: ТЕМЫ (Jira-уведомления показывают, что заведено и переназначено) ---"
psql <<SQL
SELECT to_char(occurred_at + interval '7 hours','HH24:MI') || '  ' ||
       split_part(metadata->>'from',' <',1) || '  |  ' || coalesce(metadata->>'subject','')
FROM events WHERE source='gmail'
  AND occurred_at >= '$FROM' AND occurred_at < '$TO' ORDER BY occurred_at;
SQL

echo
echo "================ ДАЛЬШЕ РУКАМИ ================"
echo "Задачи брать из Jira через MCP, а не из почты:"
echo "  reporter = currentUser() AND created >= \"$DAY\"   — что поставил"
echo "  assignee = currentUser() AND updated >= \"$DAY\"   — что вёл сам"
echo "Числа у них разные, путать нельзя."

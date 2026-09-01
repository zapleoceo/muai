-- find_payments.sql — найти повторяющиеся платежи по всем источникам сразу.
--
-- Зачем отдельным файлом, а не разовым запросом в чат: сумма в переписке
-- пишется десятком способов («5 млн», «5кк», «5.000.000», «5 000 000», «5tr»),
-- и каждый раз собирать регулярку заново — значит каждый раз промахиваться
-- мимо половины сообщений. Разбор суммы здесь один и общий.
--
-- Ищет ПО ВСЕМ источникам (telegram, slack, gmail, voice, claude_chat):
-- договорённость может прозвучать голосом на созвоне, оплата уйти из одного
-- чата, а подтверждение прийти в другой — это разные события в разные дни, и
-- поодиночке ни одно из них не находится.
--
-- Запуск:
--   docker exec -i vera3-postgres psql -U vera -d vera \
--     -v topic="сцен|stage" -v who="игор" -v since="2025-08-01" \
--     -f - < vera3/scripts/find_payments.sql
--
--   topic — за что платим (регулярка), '' = любые платежи
--   who   — имя/ник получателя; НЕ фильтр, а пометка в колонке «упомянут»
--   since — начало окна (умолчание 2000-01-01), until — конец (умолчание now)
--
-- Первым делом скрипт печатает ПОКРЫТИЕ по источникам за окно. Без этого
-- пустой результат читается как «платежей не было», хотя значит «источник в
-- это время ещё не подключали»: телеграм добэкфилен до 2025-06-01, а slack,
-- trello и голос появились в августе 2026 и берут всего 7 дней назад.
--
-- Почему `who` не фильтрует: платёж за одно и то же обсуждают то в личке с
-- человеком, то в рабочем чате, где его имени нет вовсе. Проверено на выборке:
-- сужение по имени выбрасывало треть платежей — те самые, что ушли из другого
-- чата. Отбрасывать их молча нельзя, поэтому имя только помечает строку.
--
-- Ограничение, о котором надо знать: сумма берётся из ТЕКСТА сообщения.
-- Перевод, о котором написали «отправил» без числа, попадёт в список, но со
-- ставит сумму пустой и посчитается отдельной колонкой «без суммы» — это не
-- ноль, это «сумма есть, но не в переписке».

\if :{?who}
\else
    \set who ''
\endif
\if :{?topic}
\else
    \set topic ''
\endif
\if :{?since}
\else
    \set since '2000-01-01'
\endif
\if :{?until}
\else
    \set until '2100-01-01'
\endif

\set ON_ERROR_STOP on

-- ── 0. Покрытие: что вообще есть за это окно ────────────────────────────
--
-- Стоит первым нарочно. Пустой список платежей значит одно из двух — их не
-- было, либо источник тогда ещё не писал; различить это можно только здесь.
\echo ''
\echo '=== Покрытие источников за окно ==='
SELECT
    source                 AS источник,
    count(*)               AS событий,
    min(occurred_at)::date AS первое,
    max(occurred_at)::date AS последнее
FROM events
WHERE occurred_at >= :'since'::timestamp
  AND occurred_at <  :'until'::timestamp
GROUP BY 1
ORDER BY событий DESC;

-- Число и множитель («5 млн», «5кк», «5tr») либо уже полное число со
-- разделителями («5.000.000»). Порог в три группы отсекает бытовые «45.000».
\set re_mil  '(\\d+(?:[.,]\\d+)?)\\s*(кк|к\\.к|млн|млн\\.|лям\\w*|tr|triệu)\\M'
\set re_full '\\m(\\d{1,3}(?:[ .,]\\d{3}){2,})\\M'
\set re_pay  '(плат|оплат|перевёл|перевел|перевод|скинул|отправил|внёс|внес|заплат|отдал|transfer|paid)'

-- ── 1. Кто это: где человек встречается и сколько от него событий ────────
--
-- Окно дат тут НЕ применяется намеренно: понять, кто такой человек и под
-- какими никами он ходит, нужно по всей истории, а не по периоду платежей.
\echo ''
\echo '=== Кандидаты на получателя ==='
SELECT
    metadata->>'chat_title'      AS чат,
    metadata->>'author_label'    AS автор,
    metadata->>'sender_username' AS ник,
    source                       AS источник,
    count(*)                     AS событий,
    min(occurred_at)::date       AS с,
    max(occurred_at)::date       AS по
FROM events
WHERE :'who' <> ''
  AND (
        metadata->>'chat_title'      ILIKE '%' || :'who' || '%'
     OR metadata->>'author_label'    ILIKE '%' || :'who' || '%'
     OR metadata->>'sender_username' ILIKE '%' || :'who' || '%'
  )
GROUP BY 1, 2, 3, 4
ORDER BY событий DESC
LIMIT 25;

-- ── 2. Упоминания платежей, хронологически ──────────────────────────────
--
-- `flat` схлопывает переносы строк: сумма и предмет платежа часто стоят на
-- разных строках одного сообщения, и построчная регулярка их не свяжет.
CREATE TEMP VIEW payment_hits AS
WITH flat AS (
    SELECT
        occurred_at,
        source,
        metadata->>'chat_title'   AS chat_title,
        metadata->>'author_label' AS author,
        metadata->>'author_role'  AS role,
        regexp_replace(replace(content_text, chr(10), ' '), '\s+', ' ', 'g') AS text
    FROM events
    WHERE occurred_at >= :'since'::timestamp
      AND occurred_at <  :'until'::timestamp
),
hits AS (
    SELECT *
    FROM flat
    WHERE (:'topic' = '' OR text ~* :'topic')
      AND text ~* :'re_pay'
)
SELECT
    occurred_at,
    source,
    chat_title,
    author,
    role,
    text,
    :'who' <> '' AND (
        coalesce(chat_title, '') ILIKE '%' || :'who' || '%'
     OR coalesce(author, '')     ILIKE '%' || :'who' || '%'
     OR text                     ILIKE '%' || :'who' || '%'
    ) AS mentions_who,
    CASE
        WHEN (regexp_match(text, :'re_mil', 'i')) IS NOT NULL
            THEN round(replace((regexp_match(text, :'re_mil', 'i'))[1], ',', '.')::numeric * 1000000)
        WHEN (regexp_match(text, :'re_full')) IS NOT NULL
            THEN regexp_replace((regexp_match(text, :'re_full'))[1], '[ .,]', '', 'g')::numeric
    END AS amount
FROM hits;

\echo ''
\echo '=== Упоминания платежей ==='
SELECT
    occurred_at::date AS дата,
    source            AS источник,
    chat_title        AS чат,
    coalesce(author, '?') || ' [' || coalesce(role, '?') || ']' AS автор,
    CASE WHEN mentions_who THEN '✓' ELSE '' END AS упомянут,
    amount            AS сумма,
    left(text, 220)   AS текст
FROM payment_hits
ORDER BY occurred_at;

-- ── 3. Помесячная сводка — то, ради чего всё затевалось ─────────────────
--
-- «Без суммы» показано рядом нарочно: молча выкинуть такие сообщения значило
-- бы соврать про полноту месяца.
\echo ''
\echo '=== По месяцам ==='
SELECT
    to_char(occurred_at, 'YYYY-MM')           AS месяц,
    count(*) FILTER (WHERE amount IS NOT NULL) AS платежей,
    sum(amount)                                AS итого,
    count(*) FILTER (WHERE amount IS NULL)     AS "без суммы"
FROM payment_hits
GROUP BY 1
ORDER BY 1;

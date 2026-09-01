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
--     -v topic="сцен|stage" -v who="игор" \
--     -f - < vera3/scripts/find_payments.sql
--
--   topic — за что платим (регулярка), '' = любые платежи
--   who   — имя/ник получателя; НЕ фильтр, а пометка в колонке «упомянут»
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

\set ON_ERROR_STOP on

-- Число и множитель («5 млн», «5кк», «5tr») либо уже полное число со
-- разделителями («5.000.000»). Порог в три группы отсекает бытовые «45.000».
\set re_mil  '(\\d+(?:[.,]\\d+)?)\\s*(кк|к\\.к|млн|млн\\.|лям\\w*|tr|triệu)\\M'
\set re_full '\\m(\\d{1,3}(?:[ .,]\\d{3}){2,})\\M'
\set re_pay  '(плат|оплат|перевёл|перевел|перевод|скинул|отправил|внёс|внес|заплат|отдал|transfer|paid)'

-- ── 1. Кто это: где человек встречается и сколько от него событий ────────
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

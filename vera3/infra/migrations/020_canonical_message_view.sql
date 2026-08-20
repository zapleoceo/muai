-- 020: канонический слой чтения сообщений.
--
-- Проблема, которую он закрывает: каждый потребитель (отчёты, brain-search,
-- ad-hoc SQL) заново собирал «кто кому написал» из metadata и регулярно
-- ошибался — chat_title в личке это СОБЕСЕДНИК, а не автор. Плюс сам
-- chat_title не уникален: «Дмитрий» это 8 разных чатов, «Алексей» — 5.
--
-- Решение: одна вьюха, которая отдаёт уже собранную строку. Ниже по течению
-- никто не реконструирует направление руками.

CREATE OR REPLACE VIEW v_messages AS
SELECT
    e.id,
    e.source,
    e.occurred_at                                   AS ts_utc,
    e.occurred_at + interval '7 hours'              AS ts_wib,
    (e.metadata->>'chat_id')                        AS chat_id,
    (e.metadata->>'chat_title')                     AS chat_title,
    (e.metadata->>'chat_kind')                      AS chat_kind,

    -- Устойчивый ключ собеседника: заголовок сам по себе не уникален,
    -- поэтому к нему всегда цепляется chat_id. Для gmail chat_id нет —
    -- ключом становится адрес.
    CASE
        WHEN e.metadata->>'chat_id' IS NOT NULL
            THEN coalesce(e.metadata->>'chat_title', '(без названия)')
                 || ' #' || (e.metadata->>'chat_id')
        WHEN e.source = 'gmail'
            THEN coalesce(e.metadata->>'author_label', '(неизвестно)')
        ELSE coalesce(e.metadata->>'chat_title', e.source)
    END                                             AS peer_key,

    CASE e.metadata->>'author_role'
        WHEN 'self'         THEN 'out'
        WHEN 'counterparty' THEN 'in'
        ELSE 'unknown'
    END                                             AS direction,

    -- Кто говорит. Единственный источник правды — author_role.
    CASE
        WHEN e.metadata->>'author_role' = 'self' THEN 'ДИМА'
        ELSE coalesce(
            nullif(e.metadata->>'author_label', ''),
            nullif(e.metadata->>'chat_title', ''),
            '(неизвестный автор)')
    END                                             AS speaker,

    -- Кому. В личке исходящее адресовано собеседнику, входящее — Диме.
    -- В группе адресат — сама группа в обе стороны.
    CASE
        WHEN e.metadata->>'chat_kind' IN ('group', 'channel')
            THEN coalesce(e.metadata->>'chat_title', '(группа)')
        WHEN e.metadata->>'author_role' = 'self'
            THEN coalesce(
                nullif(e.metadata->>'to', ''),
                nullif(e.metadata->>'chat_title', ''),
                '(неизвестный адресат)')
        ELSE 'ДИМА'
    END                                             AS addressee,

    -- Тело без служебной шапки ингестора.
    CASE
        WHEN position(E'\n---\n' in e.content_text) > 0
            THEN substring(e.content_text from position(E'\n---\n' in e.content_text) + 5)
        ELSE e.content_text
    END                                             AS body,

    e.content_text                                  AS raw_text,
    e.category,
    e.project
FROM events e;

COMMENT ON VIEW v_messages IS
 'Канонический слой чтения. Направление и автор уже разрешены: speaker/addressee/direction. Никогда не собирай их из metadata руками.';

-- Готовая к печати строка: «13:09 · ДИМА --> Алексей #364270031: текст».
-- Отчёты читают ТОЛЬКО её, поэтому перепутать направление физически нельзя.
CREATE OR REPLACE VIEW v_message_lines AS
SELECT
    m.id,
    m.source,
    m.ts_utc,
    m.ts_wib,
    m.peer_key,
    m.chat_title,
    m.chat_kind,
    m.direction,
    m.speaker,
    m.addressee,
    to_char(m.ts_wib, 'MM-DD HH24:MI')
        || ' · ' || m.speaker || ' --> ' || m.addressee
        || ' [' || m.peer_key || ']: '
        || regexp_replace(coalesce(m.body, ''), '\s+', ' ', 'g') AS line,
    m.body
FROM v_messages m;

COMMENT ON VIEW v_message_lines IS
 'Отчёты читают эту вьюху. Строка уже содержит направление — реконструировать его нельзя и не нужно.';

-- Диагностика: одинаковые названия у разных чатов. Из-за них в отчёт
-- 2026-08-11 попал чужой человек под знакомым именем.
CREATE OR REPLACE VIEW v_chat_title_collisions AS
SELECT
    metadata->>'chat_title'                       AS chat_title,
    count(DISTINCT metadata->>'chat_id')          AS distinct_chats,
    array_agg(DISTINCT metadata->>'chat_id')      AS chat_ids,
    max(occurred_at)                              AS last_seen
FROM events
WHERE source = 'telegram'
  AND metadata->>'chat_title' IS NOT NULL
  AND metadata->>'chat_id' IS NOT NULL
GROUP BY 1
HAVING count(DISTINCT metadata->>'chat_id') > 1;

COMMENT ON VIEW v_chat_title_collisions IS
 'Названия чатов, за которыми стоит больше одного собеседника. Фильтровать по chat_title без chat_id для таких имён нельзя.';

-- Диагностика: события без разрешимого авторства.
CREATE OR REPLACE VIEW v_messages_without_author AS
SELECT source, count(*) AS rows, max(occurred_at) AS last_seen
FROM events
WHERE metadata->>'author_role' IS NULL
GROUP BY 1;

COMMENT ON VIEW v_messages_without_author IS
 'Источники, которые не проставляют author_role. Для них speaker в v_messages = «(неизвестный автор)».';

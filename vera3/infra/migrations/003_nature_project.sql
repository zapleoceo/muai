-- Migration 003: events.nature + events.project — системная классификация.
--
-- nature: world_event | my_intent | conversation_with_me | derived_fact
--   Что это: событие мира / запрос Димы к AI / разговор с Верой / выведенный факт.
-- project: itstep | veranda | family | personal | news | other
--   К какому проекту/сфере относится.
--
-- Новые события классифицирует триаж (LLM по содержимому).
-- История засеивается детерминированно ниже (source/chat/account) —
-- одноразовый seed, дальше Вера понимает сама.

BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS nature  VARCHAR(24);
ALTER TABLE events ADD COLUMN IF NOT EXISTS project VARCHAR(24);

CREATE INDEX IF NOT EXISTS ix_events_project ON events (project);
CREATE INDEX IF NOT EXISTS ix_events_nature  ON events (nature);

-- ── nature: детерминирован по source ─────────────────────────────────────
UPDATE events SET nature = CASE source
    WHEN 'vera_chat'   THEN 'conversation_with_me'
    WHEN 'perplexity'  THEN 'my_intent'
    WHEN 'vera_memory' THEN 'derived_fact'
    ELSE 'world_event'
END
WHERE nature IS NULL;

-- ── project: seed по рабочим чатам/ящикам ─────────────────────────────────
UPDATE events SET project = 'itstep'
WHERE project IS NULL AND (
    account ILIKE '%itstep.org%'
    OR metadata->>'chat_title' IN (
        'Старшие и отчеты', 'J Branch Internal', 'Studing Jakarta internal',
        'IT-Step x TEO', 'Jakarta sales')
);

UPDATE events SET project = 'veranda'
WHERE project IS NULL AND metadata->>'chat_title' IN (
    'Veranda менеджмент', 'Веранда сотрудники', 'Veranda transactions',
    'Veranda AI', 'GameZone & Veranda', 'VerandaBot', 'Oleksandr poster Zhmirko'
);

-- Каналы (NEXTA, Україна Сейчас и т.п.) — новости
UPDATE events SET project = 'news'
WHERE project IS NULL AND source = 'telegram'
  AND metadata->>'chat_type' = 'channel';

-- Семья: личка с женой/дочкой/мамой
UPDATE events SET project = 'family'
WHERE project IS NULL AND source = 'telegram'
  AND metadata->>'chat_title' IN ('Евочка Моя Королева', 'Lisa', 'Mама');

-- Остальное — other (не NULL, чтобы запросы WHERE project='x' работали по индексу)
UPDATE events SET project = 'other' WHERE project IS NULL;

COMMIT;

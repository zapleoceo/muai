-- 045: pair_role_inferences — роли пары, выведенные МОДЕЛЬЮ из истории переписки.
--
-- Роль извлекается из одного сообщения, поэтому очевидное («мой директор»)
-- никто не пишет прямым текстом и граф знал лишь «работает с». Фоновая задача
-- brain-triage (`pair_roles_loop`) собирает по устоявшейся паре пакет улик из
-- `event_entities` (личка, письма, обращения друг к другу, упоминания третьими
-- лицами) и спрашивает брокера, какие роли из канонического набора там видны.
-- Строки здесь — ПРОИЗВОДНЫЕ данные: не правка владельца (журнала нет),
-- пересчитываются при смене пакета улик (`evidence_hash`) или по возрасту, модель связи
-- читает их как улику со сниженным весом, а ручные правки и отвергнутые роли
-- (`connection_suppressions`) их перекрывают.
--
-- direction относительно упорядоченной пары (entity_a < entity_b):
--   a_to_b — предикат читается «a <предикат> b» (a — начальник b),
--   b_to_a — «b <предикат> a», both — симметричная роль.
-- pair_role_runs — по строке на пару: хэш пакета, маркер статистики пары (чтобы цикл не
-- строил пакет по каждой паре зря), резюме, цена. Пара без найденных ролей тоже получает
-- строку, иначе её пересчитывали бы каждый цикл. Ответ модели не по схеме — `failures` + пауза
-- `retry_after` (степень двойки часов, не больше недели): одна капризная пара не стопорит очередь.
--
-- Роли vera_ro таблицы НЕ выданы: в цитатах — личная переписка.
--
-- Откат: DROP TABLE pair_role_inferences, pair_role_runs; DELETE FROM
-- schema_migrations WHERE version='045_pair_role_inferences'. Код без таблиц
-- работает: чтение возвращает пусто, роли судятся как раньше.

BEGIN;

CREATE TABLE IF NOT EXISTS pair_role_inferences (
    entity_a      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate     VARCHAR(80) NOT NULL,
    direction     VARCHAR(8) NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL,
    rationale     TEXT NOT NULL DEFAULT '',
    quotes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    model         VARCHAR(120) NOT NULL DEFAULT '',
    evidence_hash VARCHAR(64) NOT NULL,
    computed_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a, entity_b, predicate, direction),
    CHECK (entity_a < entity_b),
    CHECK (direction IN ('a_to_b', 'b_to_a', 'both'))
);

CREATE INDEX IF NOT EXISTS ix_pair_role_inferences_b ON pair_role_inferences (entity_b);

CREATE TABLE IF NOT EXISTS pair_role_runs (
    entity_a      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    evidence_hash VARCHAR(64) NOT NULL,
    pair_marker   VARCHAR(60) NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    roles_found   INTEGER NOT NULL DEFAULT 0,
    model         VARCHAR(120) NOT NULL DEFAULT '',
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    retry_after   TIMESTAMP,
    computed_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a, entity_b),
    CHECK (entity_a < entity_b)
);

CREATE INDEX IF NOT EXISTS ix_pair_role_runs_computed ON pair_role_runs (computed_at);

INSERT INTO schema_migrations (version, note)
VALUES ('045_pair_role_inferences',
        'роли пары, выведенные моделью из истории переписки (производные, пересчитываются)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

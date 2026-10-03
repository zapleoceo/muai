-- 041: connection_suppressions — «это неверно» для выведенной роли пары.
--
-- Роль «работает с» может быть выведена из общения без единой строки в
-- `relationships`: гасить в ней нечего. Запись здесь велит модели связи не
-- считать общение этой пары уликой роли (читает vera_shared.graph.suppressions).
-- Правка идёт через журнал mcp_audit (target_kind='suppression') и откатывается
-- как любая другая. Таблица не выдана роли vera_ro: это решения владельца.
--
-- Откат: DROP TABLE connection_suppressions; DELETE FROM schema_migrations
-- WHERE version='041_connection_suppressions'. Код без таблицы работает: чтение
-- возвращает пусто, роли выводятся как раньше.

BEGIN;

CREATE TABLE IF NOT EXISTS connection_suppressions (
    entity_a   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate  VARCHAR(80) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a, entity_b, predicate),
    CHECK (entity_a < entity_b)
);

CREATE INDEX IF NOT EXISTS ix_connection_suppressions_b ON connection_suppressions (entity_b);

INSERT INTO schema_migrations (version, note)
VALUES ('041_connection_suppressions',
        'отвергнутые владельцем выведенные роли пар (работает с)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

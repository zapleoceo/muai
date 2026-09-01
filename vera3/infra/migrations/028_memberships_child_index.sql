-- Migration 028: индекс по memberships.child_entity_id.
--
-- Обе стороны `relationships` проиндексированы (ix_rel_subject, ix_rel_object,
-- см. infra/sql/graph_substrate.sql), а у `memberships` — только родитель
-- (ix_membership_parent). Единственный другой индекс, который её касается, —
-- составной UNIQUE (parent_entity_id, child_entity_id, source): ведущая
-- колонка там parent, поэтому поиск по одному child_entity_id им пользоваться
-- не может и уходит в seq scan.
--
-- Кто по нему ходит:
--   * graph_repo.graph_snapshot() — степень узла и ego-соседи (обе половины
--     запроса: `child_entity_id = :fid` и подсчёт степени по набору узлов,
--     до GRAPH_MAX_NODES=800 за показ страницы /graph);
--   * graph_repo.upsert_membership / dedup.merge_entities — поиск членств
--     сливаемой сущности по её child-стороне.
--
-- Таблица маленькая (членства, не события), поэтому обычный CREATE INDEX
-- внутри транзакции здесь безопасен — в отличие от 014, где пришлось идти
-- CONCURRENTLY по 400-тысячной events.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_membership_child ON memberships (child_entity_id);

INSERT INTO schema_migrations (version, note)
VALUES ('028_memberships_child_index', 'ix_membership_child')
ON CONFLICT (version) DO NOTHING;

COMMIT;

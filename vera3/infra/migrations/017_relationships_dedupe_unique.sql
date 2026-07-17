-- 017: relationships — убрать накопленные дубли/самопетли от merge_entities
-- и закрыть их на уровне БД. merge_entities двигал subject/object без
-- guard'ов: связь merged→keeper превращалась в keeper→keeper (самопетля),
-- одинаковые связи с двух слитых сущностей — в дубли.

BEGIN;

-- 1. Самопетли — мусор по определению
DELETE FROM relationships WHERE subject_entity_id = object_entity_id;

-- 2. Дубли (subject, predicate, object): оставляем самую свежую запись
DELETE FROM relationships r
USING relationships r2
WHERE r.subject_entity_id = r2.subject_entity_id
  AND r.predicate = r2.predicate
  AND r.object_entity_id = r2.object_entity_id
  AND r.id < r2.id;

-- 3. Больше не появятся
CREATE UNIQUE INDEX IF NOT EXISTS uq_relationships_spo
  ON relationships (subject_entity_id, predicate, object_entity_id);

COMMIT;

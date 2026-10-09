-- 051: трекер задач комнаты — responsible: метка ответственного (человек/агент).
--
-- Отличается от owner: owner — служебное («owner», пока вопрос ждёт владельца), responsible — смысловое.
-- Колонка только добавляется, NULL по умолчанию — таблица не переписывается, старый код не страдает.
--
-- Откат: ALTER TABLE room_tasks DROP COLUMN responsible.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS responsible VARCHAR(128);

INSERT INTO schema_migrations (version, note)
VALUES ('051_room_task_responsible', 'трекер задач: responsible — метка ответственного')
ON CONFLICT (version) DO NOTHING;

COMMIT;

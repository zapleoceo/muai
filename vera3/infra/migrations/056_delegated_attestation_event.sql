-- 056: вид события 'delegated_attestation' — dot подтверждает в своём чате авторство
-- одной голосовой просьбы (room_task_attest). Меняется только CHECK
-- ck_room_task_events_kind на room_task_events: колонок и таблиц не добавляется.
--
-- Блокировки (проверены тестом на живом PG, tests/integration/test_attest_migration_pg.py):
--   шаг 1 (DROP + ADD ... NOT VALID) берёт ACCESS EXCLUSIVE на room_task_events —
--   читатели и писатели ждут; поэтому шаг короткий и с lock_timeout 5s: не получили
--   блокировку за 5 с — шаг целиком откатывается, ограничение остаётся прежним;
--   шаг 2 (VALIDATE) идёт отдельной транзакцией под SHARE UPDATE EXCLUSIVE.
-- Порядок выкатки: миграция ДО кода, пишущего этот вид (иначе запись упадёт на CHECK).
-- Код пишет его только при ROOM_ATTEST_ENABLED=1, по умолчанию выключено.
--
-- Откат на проде: ROOM_ATTEST_ENABLED=0. Вид события, CHECK и записанные события
-- остаются как журнал аудита; деструктивного отката на проде нет.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS ck_room_task_events_kind;
ALTER TABLE room_task_events ADD CONSTRAINT ck_room_task_events_kind
    CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed', 'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer', 'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired', 'watchdog_action', 'waiting', 'delegated_attestation')) NOT VALID;

COMMIT;

BEGIN;

ALTER TABLE room_task_events VALIDATE CONSTRAINT ck_room_task_events_kind;

INSERT INTO schema_migrations (version, note)
VALUES ('056_delegated_attestation_event', 'вид события delegated_attestation (room_task_attest)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- 054: office intake — происхождение задач внешнего источника и идемпотентные ответы.
--
-- Новые таблицы, старый код не затронут. origin и result_recipient пишутся один раз
-- (обновления в коде нет: origin, result_recipient, context, attachments); меняется только delivered_revision. Откат: ничего не удаляем.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS room_task_origin (
    room varchar(64) NOT NULL,
    task_id varchar(128) NOT NULL,
    creator_principal varchar(64) NOT NULL,
    client_task_id varchar(128) NOT NULL,
    payload_hash varchar(64) NOT NULL,
    origin jsonb NOT NULL,
    result_recipient jsonb NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
    correlation_id varchar(128),
    causation_id varchar(128),
    delivered_revision bigint NOT NULL DEFAULT 0,
    created_at timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (room, task_id),
    CONSTRAINT uq_room_task_origin_client UNIQUE (creator_principal, client_task_id)
);

CREATE INDEX IF NOT EXISTS ix_room_task_origin_creator
    ON room_task_origin (creator_principal, room, task_id);

CREATE TABLE IF NOT EXISTS office_answer_keys (
    id bigserial PRIMARY KEY,
    room varchar(64) NOT NULL,
    task_id varchar(128) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    qid bigint NOT NULL,
    text_hash varchar(64) NOT NULL,
    revision_after bigint,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_office_answer_keys UNIQUE (task_id, idempotency_key)
);

COMMIT;

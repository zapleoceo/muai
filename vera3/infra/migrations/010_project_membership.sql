-- Migration 010: project_membership — источник истины принадлежности к проектам.
--
-- Синхронизируется из папок Telegram (folder:ItStep → itstep) и правил имён
-- (name:veranda). Люди (kind=person) выводятся из участия в чатах проекта.
-- events.project проставляется из этой таблицы (kind IN chat/account).
--
-- kind:  chat    key = abs(chat_id)      label = chat_title
--        person  key = tg sender_id      label = username/name
--        account key = ILIKE-паттерн     label = проект-почта
-- Человек может относиться к нескольким проектам (несколько строк) — PK это
-- допускает: (project, kind, key).

BEGIN;

CREATE TABLE IF NOT EXISTS project_membership (
    project    VARCHAR(24)  NOT NULL,
    kind       VARCHAR(10)  NOT NULL,   -- chat | person | account
    key        VARCHAR(64)  NOT NULL,
    label      TEXT,
    source     VARCHAR(60)  NOT NULL,   -- folder:ItStep | name:veranda | derived:chat
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (project, kind, key)
);

CREATE INDEX IF NOT EXISTS ix_pm_kind_key ON project_membership (kind, key);
CREATE INDEX IF NOT EXISTS ix_pm_person   ON project_membership (key) WHERE kind = 'person';

COMMIT;

-- 026: claude_session_queue — сессии Claude Code ждут осмысления здесь.
--
-- Осмыслить сессию в самом HTTP-запросе нельзя, и это измерено, а не
-- предположено: одно окно на 21 тыс. символов не уложилось в 120с ожидания
-- брокера («job still pending after 120s»), а nginx на location /v1/ обрывает
-- запрос по дефолтным 60с (504 пришёл ровно на 60.8с). Большая сессия — это до
-- 20 окон, то есть десятки минут: столько не держит ни один прокси.
--
-- Поэтому шлюз принимает сессию, кладёт сюда и отвечает 202. Фоновый воркер
-- осмысляет с большим ожиданием и пишет событие. Клиент на ноутбуке двигает
-- свой курсор только когда статус стал done — иначе сессия потерялась бы
-- молча, как это было с gmail-курсором и trello.
--
-- Приватность: turns это сырая переписка, и она лежит здесь ТОЛЬКО до
-- осмысления. Воркер очищает поле сразу после записи события.

BEGIN;

CREATE TABLE IF NOT EXISTS claude_session_queue (
    session_id   VARCHAR(64) PRIMARY KEY,
    project_dir  VARCHAR(255) NOT NULL DEFAULT '',
    cwd          TEXT,
    git_branch   VARCHAR(255),
    started_at   TIMESTAMP NOT NULL,
    ended_at     TIMESTAMP NOT NULL,
    turns        JSONB NOT NULL DEFAULT '[]'::jsonb,
    turn_count   INTEGER NOT NULL DEFAULT 0,
    -- pending → processing → done | error
    status       VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    event_id     BIGINT,
    -- Сколько реплик уже осмыслено: клиент сравнивает со своим числом и
    -- двигает курсор только когда сервер догнал.
    done_turns   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_claude_queue_status
    ON claude_session_queue (status, created_at);

COMMIT;

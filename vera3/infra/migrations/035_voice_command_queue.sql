-- 035: voice_command_queue — голосовые поручения владельца ждут ответа здесь.
--
-- Слушатель на ноутбуке ловит «Вера, мне нужна помощь, <поручение>» на
-- дорожке микрофона и шлёт текст в POST /v1/voice/command. Шлюз пишет событие
-- (source = 'voice_command' — видно в истории и в поиске) и строку сюда, бот
-- забирает её (FOR UPDATE SKIP LOCKED), отвечает владельцу в Telegram и
-- помечает done. Та же схема, что у claude_session_queue (026): второй шины
-- между сервисами нет.
--
-- command_id выдаёт слушатель — ретрай той же команды из его офлайн-очереди
-- упирается в PRIMARY KEY и второго ответа не даёт.
--
-- Приватность: instruction нужна только до ответа, бот очищает её на done, а
-- на error — как только сообщил владельцу, что именно не вышло (notified_at).
-- Сам текст остаётся в событии. В колонку error пишется только тип/код.

BEGIN;

CREATE TABLE IF NOT EXISTS voice_command_queue (
    command_id   VARCHAR(64) PRIMARY KEY,
    event_id     BIGINT,
    instruction  TEXT NOT NULL DEFAULT '',
    spoken_at    TIMESTAMP NOT NULL,
    -- pending → processing → done | error
    status       VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    -- «Услышала, делаю» уже ушло: ретрай после сбоя шлёт только результат.
    acked_at     TIMESTAMP,
    -- Ответ уже ушёл: после перезапуска бот второй раз не отвечает.
    answered_at  TIMESTAMP,
    -- Ретрай с паузой (30 с, 1 мин, 2 мин), а не три попытки подряд.
    next_attempt_at TIMESTAMP,
    -- Владельцу сообщили об ошибке; NULL при status='error' — сообщить.
    notified_at  TIMESTAMP,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Ранняя редакция 035 создавала таблицу без этих колонок. CREATE TABLE IF
-- NOT EXISTS их на существующую таблицу не добавит — поэтому явно.
ALTER TABLE voice_command_queue
    ADD COLUMN IF NOT EXISTS answered_at     TIMESTAMP,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS notified_at     TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_voice_command_status
    ON voice_command_queue (status, created_at);

INSERT INTO schema_migrations (version, note)
VALUES ('035_voice_command_queue',
        'очередь голосовых поручений: шлюз кладёт, бот отвечает владельцу')
ON CONFLICT (version) DO NOTHING;

COMMIT;

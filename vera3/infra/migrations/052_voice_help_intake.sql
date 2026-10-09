-- 052: voice_command_queue — срочная просьба голосом заводит задачу в комнате.
--
-- «Вера, мне нужна помощь, <что случилось>» теперь не только ответ мозга, но
-- и срочная задача комнаты (help-<command_id>). Здесь — то, что нужно очереди,
-- чтобы провести просьбу до «Взял: <агент>»:
--   kind          — command | reprompt («Не расслышала поручение, повтори»);
--   confidence    — уверенность слушателя 0..1 (NULL — старый слушатель);
--   source        — откуда: session_id, смещения, app/window (без текста);
--   help_state    — путь просьбы: confirm → asked → ready → opened →
--                   escalated → reminded → taken | declined | expired;
--   task_id       — задача в комнате; task_opened_at — от неё считаются
--                   5 мин до эскалации dot и 20 мин до напоминания;
--   confirm_asked_at — «Это ты сказал?» ушло: 10 мин на ответ, потом отмена;
--   taken_by / taken_at / escalated_at / reminded_at — что уже сообщили
--                   владельцу: после перезапуска бот не шлёт то же второй раз.
--
-- Колонки только добавляются, NULL/дефолт — старый код не страдает.
-- Откат: ALTER TABLE voice_command_queue DROP COLUMN … (по списку ниже).

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE voice_command_queue
    ADD COLUMN IF NOT EXISTS kind             VARCHAR(16) NOT NULL DEFAULT 'command',
    ADD COLUMN IF NOT EXISTS confidence       REAL,
    ADD COLUMN IF NOT EXISTS source           JSONB,
    ADD COLUMN IF NOT EXISTS help_state       VARCHAR(16),
    ADD COLUMN IF NOT EXISTS task_id          VARCHAR(128),
    ADD COLUMN IF NOT EXISTS task_opened_at   TIMESTAMP,
    ADD COLUMN IF NOT EXISTS confirm_asked_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS taken_by         VARCHAR(64),
    ADD COLUMN IF NOT EXISTS taken_at         TIMESTAMP,
    ADD COLUMN IF NOT EXISTS escalated_at     TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reminded_at      TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_voice_command_help_state
    ON voice_command_queue (help_state)
    WHERE help_state IS NOT NULL;

INSERT INTO schema_migrations (version, note)
VALUES ('052_voice_help_intake',
        'голосовая срочная просьба: задача в комнате, подтверждение, эскалация')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- 044: voice_speaker_map — кто скрывается за ярлыком голоса в созвоне.
--
-- Слушатель на ноутбуке хранит отпечатки голосов (`voiceprints.json`) у себя; на сервер
-- уходят только имена говорящих в репликах («Оля К.», «Собеседник 2»). Опознать
-- «Собеседник 2» сервер не может, а владелец или агент — может. Таблица связывает:
--   kind='event'      key='<event_id>:<ярлык говорящего>' → сущность (для одного созвона);
--   kind='voiceprint' key='<voiceprint_id>'               → сущность (для будущих версий
--                     слушателя, присылающих id отпечатка в реплике: один раз назвали —
--                     узнаётся в каждом созвоне).
-- Решение владельца: пересчёт связей созвона его читает, `event_entities` получает
-- участника с source_of_link='manual' (kind='event') или 'voiceprint'.
--
-- Роли vera_ro таблица не выдана. Откат: DROP TABLE voice_speaker_map; DELETE FROM
-- schema_migrations WHERE version='044_voice_speaker_map'.

BEGIN;

CREATE TABLE IF NOT EXISTS voice_speaker_map (
    kind       VARCHAR(12) NOT NULL,
    key        VARCHAR(200) NOT NULL,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, key),
    CHECK (kind IN ('event', 'voiceprint'))
);

CREATE INDEX IF NOT EXISTS ix_voice_speaker_map_entity ON voice_speaker_map (entity_id);

INSERT INTO schema_migrations (version, note)
VALUES ('044_voice_speaker_map',
        'ярлык голоса в созвоне (по событию или id отпечатка) → сущность; решение владельца')
ON CONFLICT (version) DO NOTHING;

COMMIT;

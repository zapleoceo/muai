-- 027: индекс под подсчёт участия владельца в чате.
--
-- Политика распознавания фото (`vera_shared.media_policy`) спрашивает на
-- КАЖДОМ входящем медиа: сколько сообщений владелец сам написал в этом чате.
-- Без индекса это скан по jsonb-полю на 430 тысячах событий. Частичный
-- индекс — только по своим телеграм-сообщениям, поэтому маленький и точно
-- под запрос: WHERE source='telegram' AND chat_id=? AND direction='sent'.
--
-- Считается всё равно с кэшем на час (`chat_activity.TTL_S`), индекс нужен
-- на промахи кэша и на прогоны скрипта доливки очереди.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_events_tg_chat_sent
    ON events ((metadata->>'chat_id'))
    WHERE source = 'telegram' AND metadata->>'direction' = 'sent';

COMMIT;

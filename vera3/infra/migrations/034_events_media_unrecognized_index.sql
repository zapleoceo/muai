-- Частичный индекс под живой счётчик остатка распознавания
-- (vera_shared/media_backlog.py). Предикат обязан совпадать с запросом там:
-- иначе группировка снова уходит в скан всех 466 тыс. событий (1.7 с).
-- CONCURRENTLY — без BEGIN, запись в events не блокируется.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_media_unrecognized
    ON events ((metadata->>'chat_id'), (metadata->>'chat_kind'), (metadata->>'media_kind'))
    WHERE metadata->>'media_kind' IN ('audio', 'image', 'photo', 'voice')
      AND (metadata->>'media_recognition' IS NULL
           OR metadata->>'media_recognition' = 'failed')
      AND COALESCE(metadata->>'media_permanent', 'false') <> 'true'
      -- Фото вещательных каналов политика не распознаёт никогда, а это
      -- почти весь нераспознанный остаток (15 929 из 15 929 на 26.09.2026).
      AND (metadata->>'media_kind' IN ('audio', 'voice')
           OR COALESCE(metadata->>'chat_kind', '') <> 'channel');

INSERT INTO schema_migrations (version, note)
VALUES ('034_events_media_unrecognized_index',
        'частичный индекс под живой остаток распознавания в дашборде')
ON CONFLICT (version) DO NOTHING;

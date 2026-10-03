-- Карточка человека: последние сообщения из Telegram за время, а не за 4.5 с.
-- Частичный ix_events_tg_sender (только sender_id) планировщик игнорировал: оценка
-- «2259 строк» вместо реальных 176 делала выгоднее обход ix_events_occurred_at назад
-- по всем событиям с фильтром — 4.4 с, и карточка показывала «часть событий не
-- загрузилась» (04.10.2026, карточка директора). Составной ключ (кто, когда) отдаёт
-- первые N строк уже в порядке ORDER BY occurred_at DESC — сортировка и обход не нужны.
-- То же для личного чата (chat_id = id собеседника). CONCURRENTLY — без BEGIN.
-- Номер 046: 042–045 заняты ветками связей событий с людьми.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_tg_sender_time
    ON events ((metadata->>'sender_id'), occurred_at DESC)
    WHERE source = 'telegram';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_tg_chat_time
    ON events ((metadata->>'chat_id'), occurred_at DESC)
    WHERE source = 'telegram';

INSERT INTO schema_migrations (version, note)
VALUES ('046_events_tg_person_time_indexes',
        'составные индексы (отправитель/чат, время) для событий в карточке человека')
ON CONFLICT (version) DO NOTHING;

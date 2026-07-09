-- Трейс брокер-вызова в usage_log: request_id + метка ключа из ответа aibroker.
-- Плюс индекс (event_id, created_at) для джойна log-страницы дашборда.
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS request_id VARCHAR(64);
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS key_label  VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_usage_event ON usage_log (event_id, created_at);

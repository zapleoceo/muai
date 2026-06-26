-- Migration 004: честный статус Gmail-подключений.
--
-- Проблема: дашборд показывал is_active=true даже когда refresh-токен отозван
-- Google (invalid_grant). Поллер при этом каждые 5 мин стучался впустую.
--
-- needs_reauth — токен мёртв, нужно переподключение (кнопка в дашборде).
-- last_error  — текст последней ошибки refresh (для диагностики).
-- last_ok_at  — когда последний раз refresh реально прошёл.

BEGIN;

ALTER TABLE gmail_accounts ADD COLUMN IF NOT EXISTS needs_reauth BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE gmail_accounts ADD COLUMN IF NOT EXISTS last_error   TEXT;
ALTER TABLE gmail_accounts ADD COLUMN IF NOT EXISTS last_ok_at   TIMESTAMP WITHOUT TIME ZONE;

-- Текущие 3 аккаунта подтверждённо отозваны (invalid_grant, проверено вручную).
UPDATE gmail_accounts
SET needs_reauth = TRUE,
    last_error = 'invalid_grant: Token has been expired or revoked (migration 004)'
WHERE is_active = TRUE;

COMMIT;

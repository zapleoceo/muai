-- 025: slack_auth — пользовательский токен Slack под шифрованием.
--
-- Как у telegram_sessions / instagram_sessions: секрет живёт в БД под crypto,
-- а не в infra/.env. Тогда подключение источника делается из дашборда
-- (/api/slack/start), а не правкой файла на сервере по ssh.
--
-- SLACK_USER_TOKEN в окружении остаётся запасным путём: если активной строки
-- здесь нет, поллер берёт токен из env. Так уже подключённый источник не
-- отвалится от появления этой таблицы.

BEGIN;

CREATE TABLE IF NOT EXISTS slack_auth (
    id         SERIAL PRIMARY KEY,
    team_id    VARCHAR(32) NOT NULL UNIQUE,
    team_name  VARCHAR(255) NOT NULL DEFAULT '',
    user_id    VARCHAR(32) NOT NULL DEFAULT '',
    username   VARCHAR(255) NOT NULL DEFAULT '',
    token_enc  TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    last_ok_at TIMESTAMP WITHOUT TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, note)
VALUES ('025_slack_auth', 'slack_auth: user-токен под crypto, подключение из дашборда')
ON CONFLICT (version) DO NOTHING;

COMMIT;

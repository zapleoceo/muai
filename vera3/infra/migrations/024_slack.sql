-- 024: состояние обхода Slack — каналы и треды.
--
-- Токен Slack живёт в infra/.env (личный, один на всё), поэтому таблицы держат
-- только состояние обхода. last_ts — `ts` последнего разобранного сообщения:
-- у Slack это одновременно время и идентификатор сообщения в канале, так что
-- курсор не теряет хвост при всплеске активности.
--
-- slack_threads нужна отдельно, и это не украшение: ответы в тредах НЕ приходят
-- в conversations.history — она отдаёт только корневое сообщение. Больше того,
-- тред, чьё корневое сообщение старше курсора, в истории не появится вовсе,
-- сколько бы новых ответов там ни было. Без наблюдения за тредами обсуждения в
-- Slack — а решения принимаются именно в них — были бы невидимы навсегда.
--
-- Каналы заводятся сами при первом опросе; покинутый гаснет is_active=false,
-- а не удаляется — курсор переживёт возвращение.

BEGIN;

CREATE TABLE IF NOT EXISTS slack_conversations (
    conversation_id VARCHAR(32) PRIMARY KEY,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    kind            VARCHAR(16) NOT NULL DEFAULT 'channel',
    is_private      BOOLEAN NOT NULL DEFAULT FALSE,
    last_ts         VARCHAR(32),
    last_polled_at  TIMESTAMP WITHOUT TIME ZONE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_error      TEXT,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS slack_threads (
    id               SERIAL PRIMARY KEY,
    conversation_id  VARCHAR(32) NOT NULL,
    thread_ts        VARCHAR(32) NOT NULL,
    last_reply_ts    VARCHAR(32),
    last_activity_at TIMESTAMP WITHOUT TIME ZONE,
    last_polled_at   TIMESTAMP WITHOUT TIME ZONE,
    created_at       TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_slack_thread UNIQUE (conversation_id, thread_ts)
);

-- Очередь проверки тредов: живые сначала, дольше всех не проверенные первыми.
CREATE INDEX IF NOT EXISTS ix_slack_threads_due
    ON slack_threads (conversation_id, last_activity_at DESC, last_polled_at ASC);

INSERT INTO schema_migrations (version, note)
VALUES ('024_slack', 'slack_conversations + slack_threads: курсоры каналов и тредов')
ON CONFLICT (version) DO NOTHING;

COMMIT;

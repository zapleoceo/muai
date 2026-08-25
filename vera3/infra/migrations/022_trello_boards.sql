-- 022: trello_boards — курсор опроса по каждой доске.
--
-- Ключ и токен Trello живут в infra/.env (личный аккаунт, один на всё), поэтому
-- таблица держит только состояние обхода. last_action_id — id последнего
-- действия, а не дата: Trello отдаёт действия от новых к старым, и id-курсор
-- не теряет хвост при всплеске активности.
--
-- Доски заводятся сами при первом опросе; закрытая доска гаснет is_active=false,
-- а не удаляется — курсор переживёт её возвращение.

BEGIN;

CREATE TABLE IF NOT EXISTS trello_boards (
    board_id       VARCHAR(64) PRIMARY KEY,
    name           VARCHAR(255) NOT NULL DEFAULT '',
    last_action_id VARCHAR(64),
    last_polled_at TIMESTAMP WITHOUT TIME ZONE,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    last_error     TEXT,
    created_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, note)
VALUES ('022_trello_boards', 'trello_boards: курсоры досок')
ON CONFLICT (version) DO NOTHING;

COMMIT;

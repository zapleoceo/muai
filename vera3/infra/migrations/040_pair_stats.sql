-- 040: pair_stats — кэш взаимодействий пары людей для модели «связь как пара».
--
-- Считается периодической задачей brain-triage (`pair_stats_loop`) из событий
-- telegram / slack / gmail и членств; ВСЯ таблица пересобирается одной
-- транзакцией, поэтому правды тут нет — её можно очистить и пересчитать
-- (`scripts/refresh_pair_stats.py`). Нужна ради скорости: «оба писали в одном
-- чате в один день» — самосоединение по 450 тыс. событий, ~10 секунд, на
-- карточку человека такое не посчитать.
--
-- Роли vera_ro (MCP sql_query) таблица НЕ выдана намеренно: счётчики личной
-- переписки пар не должны выходить через произвольный SQL агентов.
--
-- Откат: DROP TABLE pair_stats; DELETE FROM schema_migrations WHERE
-- version='040_pair_stats'. Код без таблицы работает: чтение возвращает пусто,
-- связи считаются только по записанным ролям.

BEGIN;

CREATE TABLE IF NOT EXISTS pair_stats (
    entity_a      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dm_msgs       INTEGER NOT NULL DEFAULT 0,
    dm_days       INTEGER NOT NULL DEFAULT 0,
    mail_msgs     INTEGER NOT NULL DEFAULT 0,
    mail_days     INTEGER NOT NULL DEFAULT 0,
    co_days       INTEGER NOT NULL DEFAULT 0,
    work_co_days  INTEGER NOT NULL DEFAULT 0,
    co_chats      INTEGER NOT NULL DEFAULT 0,
    shared_groups INTEGER NOT NULL DEFAULT 0,
    active_days   INTEGER NOT NULL DEFAULT 0,
    first_at      TIMESTAMP,
    last_at       TIMESTAMP,
    computed_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a, entity_b),
    CHECK (entity_a < entity_b)
);

CREATE INDEX IF NOT EXISTS ix_pair_stats_b ON pair_stats (entity_b);

INSERT INTO schema_migrations (version, note)
VALUES ('040_pair_stats',
        'кэш взаимодействий пары (DM, общие чаты, группы): пересобирается задачей brain-triage')
ON CONFLICT (version) DO NOTHING;

COMMIT;

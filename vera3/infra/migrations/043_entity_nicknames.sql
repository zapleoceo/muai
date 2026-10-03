-- 043: entity_nicknames — прозвища и инициалы человека, действующие в ОБЛАСТИ.
--
-- «ДА» в рабочем чате — Дмитрий Александрович, а в туристическом чате — просто
-- «да» заглавными. `entity_aliases` держит идентификаторы источников (уникальные
-- по (source, identifier)) и области не знает, поэтому прозвища живут отдельно:
-- токен регистрозависим (`case_sensitive`), действует только там, где `scope_kind`
-- велит его засчитывать:
--   work     — рабочие чаты (project_membership) и личка с теми, у кого с человеком
--              устоявшаяся пара (pair_stats);
--   contacts — то же + группы, где пишут не меньше двух сильных контактов человека;
--   chats    — только чаты из `scope_ids` (ключи chat_id);
--   global   — везде (однозначные короткие имена).
-- status: suggested — предложено кодом, ждёт владельца (автоматически не применяется);
-- active — решение владельца; rejected — отвергнуто, не переспрашивается.
-- Читает индекс связей `event_entities` (миграция 042) и пакет улик `pair_roles`.
--
-- Роли vera_ro таблица не выдана. Откат: DROP TABLE entity_nicknames; DELETE FROM
-- schema_migrations WHERE version='043_entity_nicknames'. Код без таблицы работает:
-- прозвищ нет, упоминания ищутся по именам.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_nicknames (
    id             SERIAL PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    token          VARCHAR(80) NOT NULL,
    case_sensitive BOOLEAN NOT NULL DEFAULT TRUE,
    scope_kind     VARCHAR(12) NOT NULL DEFAULT 'work',
    scope_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    status         VARCHAR(12) NOT NULL DEFAULT 'suggested',
    source         VARCHAR(12) NOT NULL DEFAULT 'owner',
    reason         TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    decided_at     TIMESTAMP,
    CONSTRAINT uq_entity_nickname UNIQUE (entity_id, token),
    CHECK (scope_kind IN ('work', 'contacts', 'chats', 'global')),
    CHECK (status IN ('suggested', 'active', 'rejected'))
);

CREATE INDEX IF NOT EXISTS ix_entity_nicknames_status ON entity_nicknames (status);

INSERT INTO schema_migrations (version, note)
VALUES ('043_entity_nicknames',
        'прозвища и инициалы человека с областью действия; предложения кода ждут владельца')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- 042: event_entities — ОДНА модель связи «событие ↔ сущность» для всех источников.
--
-- До неё событие знало только автора (алиас отправителя) и, для timeline, подстроку
-- полного имени; получатель письма, собеседник лички, участники созвона и
-- упомянутые («ДА просил ознакомиться…») нигде не хранились. Теперь строка = одна
-- связь: кто (entity_id), в какой роли и откуда это известно.
--   role           author | recipient | participant | mentioned
--   source_of_link alias      — автор/получатель по алиасу (telegram user:, gmail-адрес);
--                  nickname   — прозвище из entity_nicknames (регистрозависимое, в области);
--                  voiceprint — голос в созвоне опознан слушателем (имя в реплике);
--                  name_match — имя найдено/исправлено сопоставлением (полное имя, фамилия,
--                               ASR-искажение «Арчевский» → «Корчевский»);
--                  manual     — решение владельца или агента (MCP), пересчёт его не трогает.
--   token          что именно сработало (прозвище, ярлык говорящего); '' у связей по алиасу.
--   span           где в событии (JSON: {"utterance": N} / {"speaker": "…"}), может быть NULL.
--   scope_ok       false — прозвище вне области («да» заглавными в туристическом чате):
--                  строка нужна для счёта и аудита, читатели её не берут.
-- Производные данные (кроме manual): строятся brain-triage по новым событиям
-- (`links_loop`) и scripts/backfill_event_links.py по старым — резюмируемо, пачками.
--
-- link_cursor — до какого events.id индекс построен (потоки forward и backfill).
-- Роли vera_ro таблицы не выданы: связи раскрывают личную переписку пар.
--
-- Откат: DROP TABLE event_entities, link_cursor; DELETE FROM schema_migrations
-- WHERE version='042_event_entities'. Код без таблиц работает: фильтры по сущностям
-- вернут «не поддерживается», остальное — как раньше.

BEGIN;

CREATE TABLE IF NOT EXISTS event_entities (
    event_id       BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role           VARCHAR(12) NOT NULL,
    source_of_link VARCHAR(12) NOT NULL,
    confidence     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    token          VARCHAR(120) NOT NULL DEFAULT '',
    span           JSONB,
    scope_ok       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, entity_id, role, token),
    CHECK (role IN ('author', 'recipient', 'participant', 'mentioned')),
    CHECK (source_of_link IN ('alias', 'nickname', 'voiceprint', 'name_match', 'manual'))
);

CREATE INDEX IF NOT EXISTS ix_event_entities_entity ON event_entities (entity_id, role, event_id DESC);

CREATE TABLE IF NOT EXISTS link_cursor (
    name       VARCHAR(20) PRIMARY KEY,
    event_id   BIGINT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

INSERT INTO schema_migrations (version, note)
VALUES ('042_event_entities',
        'связь событие-сущность: автор, получатель, участник, упомянутый; источник связи и уверенность')
ON CONFLICT (version) DO NOTHING;

COMMIT;
